"""
Main Orchestrator - FINAL FIX: Entry price bookkeeping + Stop loss with source checking
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
    VELOCITY_MIN_RECENT_1S_SOL, VELOCITY_MIN_RECENT_3S_SOL, VELOCITY_MAX_DROP_PERCENT,
    # NEW: Import velocity ceiling parameters
    VELOCITY_MAX_SOL_PER_SECOND, VELOCITY_MAX_RECENT_1S_SOL, VELOCITY_MAX_RECENT_3S_SOL,
    # NEW: Import momentum exit parameters
    MOMENTUM_MAX_DRAWDOWN_PP, MOMENTUM_MIN_PEAK_PERCENT,
    MOMENTUM_VELOCITY_DEATH_PERCENT, MOMENTUM_BIG_WIN_PERCENT,
    MOMENTUM_MAX_HOLD_SECONDS
)

# ✅ NEW: Import profit protection settings
try:
    from config import EXTREME_TP_PERCENT, TRAIL_START_PERCENT, TRAIL_GIVEBACK_PERCENT
except ImportError:
    # Fallback defaults if not in config yet
    EXTREME_TP_PERCENT = 150.0
    TRAIL_START_PERCENT = 100.0
    TRAIL_GIVEBACK_PERCENT = 50.0
    logger.warning("⚠️ Profit protection settings not in config.py, using defaults")

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
    """Track an active position with timer-based exit"""
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
        
        # ✅ CHATGPT FIX #4: Add source tracking and debounce fields
        self.has_chain_price = False
        self.last_price_source = "unknown"
        self.sl_chain_debounce = 0
        
        # ✅ DON'T calculate entry_token_price_sol here
        # It will be set properly in on_token_found() with correct units
        self.entry_token_price_sol = 0

class SniperBot:
    """Main sniper bot orchestrator with velocity gate, timer exits, and fail-fast"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING SNIPER BOT - VELOCITY + TIMER + FAIL-FAST")
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
            min_snapshots=1,
            # NEW: Add velocity ceiling parameters
            max_sol_per_second=VELOCITY_MAX_SOL_PER_SECOND,
            max_recent_1s_sol=VELOCITY_MAX_RECENT_1S_SOL,
            max_recent_3s_sol=VELOCITY_MAX_RECENT_3S_SOL
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
        logger.info(f"  • Strategy: VELOCITY GATE + TIMER EXIT + FAIL-FAST")
        logger.info(f"  • Velocity gate: {VELOCITY_MIN_SOL_PER_SECOND}-{VELOCITY_MAX_SOL_PER_SECOND} SOL/s avg, ≥{VELOCITY_MIN_BUYERS} buyers")
        logger.info(f"  • Bot pump rejection: >{VELOCITY_MAX_SOL_PER_SECOND} SOL/s avg or >{VELOCITY_MAX_RECENT_1S_SOL} SOL in 1s")
        logger.info(f"  • Recent velocity: ≥{VELOCITY_MIN_RECENT_1S_SOL} SOL (1s), ≥{VELOCITY_MIN_RECENT_3S_SOL} SOL (3s)")
        logger.info(f"  • Max velocity drop: {VELOCITY_MAX_DROP_PERCENT}%")
        logger.info(f"  • Timer exit: {TIMER_EXIT_BASE_SECONDS}s ±{TIMER_EXIT_VARIANCE_SECONDS}s")
        logger.info(f"  • Extension: +{TIMER_EXTENSION_SECONDS}s if >{TIMER_EXTENSION_PNL_THRESHOLD}% and accelerating")
        logger.info(f"  • Fail-fast: Exit at {FAIL_FAST_CHECK_TIME}s if P&L <{FAIL_FAST_PNL_THRESHOLD}% or velocity dead")
        logger.info(f"  • Liquidity gate: {LIQUIDITY_MULTIPLIER}x buy size (min {MIN_LIQUIDITY_SOL} SOL)")
        logger.info(f"  • Max slippage: {MAX_SLIPPAGE_PERCENT}%")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
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
    
    def _get_current_token_price(self, mint: str, curve_data: dict) -> Optional[float]:
        """
        Calculate current token price - returns lamports per atomic (same units as entry price)
        ✅ CRITICAL: Must return same units as entry price for P&L calculation
        """
        try:
            if not curve_data:
                return None
            
            # The curve_data already has price_lamports_per_atomic calculated correctly
            # Just return it directly - NO conversion!
            price_lamports_per_atomic = curve_data.get('price_lamports_per_atomic', 0)
            
            if price_lamports_per_atomic is None or price_lamports_per_atomic <= 0:
                logger.debug(f"Invalid price from curve data")
                return None
            
            logger.debug(
                f"Current price for {mint[:8]}...: "
                f"{price_lamports_per_atomic:.10f} lamports/atomic (direct from curve_data)"
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

    async def _get_transaction_proceeds_robust(
        self,
        signature: str,
        mint: str,
        max_wait: int = 30
    ) -> dict:
        """
        Robust transaction parsing with polling - reads EXACT SOL proceeds from blockchain

        This method:
        1. Polls RPC until transaction appears (up to max_wait seconds)
        2. Parses the transaction to extract EXACT SOL received and tokens sold
        3. Returns structured dict with success status and values

        This eliminates contamination from overlapping trades by reading directly from the transaction.
        """
        try:
            from solders.signature import Signature as SoldersSignature

            tx_sig = SoldersSignature.from_string(signature)
            start = time.time()

            logger.debug(f"🔍 Polling for transaction {signature[:8]}...")

            # Poll for transaction to appear
            tx = None
            while time.time() - start < max_wait:
                try:
                    # Check if transaction exists in RPC
                    status = self.trader.client.get_signature_statuses([tx_sig])

                    if status and status.value and status.value[0]:
                        # Transaction found, now fetch full details
                        tx_response = self.trader.client.get_transaction(
                            tx_sig,
                            encoding="jsonParsed",
                            max_supported_transaction_version=0
                        )

                        if tx_response and tx_response.value:
                            tx = tx_response.value
                            logger.debug(f"✓ Transaction found at {time.time() - start:.1f}s")
                            break

                except Exception as e:
                    logger.debug(f"Poll attempt failed (retrying): {e}")

                await asyncio.sleep(0.5)

            # Check if we got the transaction
            if not tx:
                wait_time = time.time() - start
                logger.warning(f"⏱️ Transaction not found after {wait_time:.1f}s timeout")
                return {
                    "success": False,
                    "sol_received": 0,
                    "tokens_sold": 0,
                    "wait_time": wait_time
                }

            # Check if transaction failed
            if tx.transaction.meta is None or tx.transaction.meta.err is not None:
                logger.error(f"❌ Transaction failed on-chain: {tx.transaction.meta.err if tx.transaction.meta else 'no meta'}")
                return {
                    "success": False,
                    "sol_received": 0,
                    "tokens_sold": 0,
                    "wait_time": time.time() - start
                }

            # Parse transaction to extract SOL and token deltas
            meta = tx.transaction.meta
            my_pubkey_str = str(self.wallet.pubkey)

            # Extract SOL delta
            sol_delta = 0.0
            account_keys = [str(key) for key in tx.transaction.transaction.message.account_keys]

            try:
                wallet_index = account_keys.index(my_pubkey_str)
                pre_sol_lamports = meta.pre_balances[wallet_index]
                post_sol_lamports = meta.post_balances[wallet_index]
                sol_delta = (post_sol_lamports - pre_sol_lamports) / 1e9
            except (ValueError, IndexError) as e:
                logger.error(f"❌ Wallet not found in transaction accounts: {e}")
                return {
                    "success": False,
                    "sol_received": 0,
                    "tokens_sold": 0,
                    "wait_time": time.time() - start
                }

            # Extract token delta
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
            tokens_sold = abs(token_delta)

            wait_time = time.time() - start

            # Success - log details
            logger.info(f"✅ Transaction parsed successfully:")
            logger.info(f"   SOL received: {sol_delta:+.6f} SOL")
            logger.info(f"   Tokens sold: {tokens_sold:,.2f}")
            logger.info(f"   Wait time: {wait_time:.1f}s")

            return {
                "success": True,
                "sol_received": sol_delta,
                "tokens_sold": tokens_sold,
                "wait_time": wait_time
            }

        except Exception as e:
            logger.error(f"❌ Error in robust transaction parsing: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {
                "success": False,
                "sol_received": 0,
                "tokens_sold": 0,
                "wait_time": time.time() - start if 'start' in locals() else 0
            }

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
                    "🚀 Bot started - VELOCITY + TIMER + FAIL-FAST\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    f"⚡ Velocity: ≥{VELOCITY_MIN_SOL_PER_SECOND} SOL/s\n"
                    f"⏱️ Timer: {TIMER_EXIT_BASE_SECONDS}s ±{TIMER_EXIT_VARIANCE_SECONDS}s\n"
                    f"⚠️ Fail-fast: {FAIL_FAST_CHECK_TIME}s @ {FAIL_FAST_PNL_THRESHOLD}%\n"
                    f"🛡️ Liquidity: {LIQUIDITY_MULTIPLIER}x\n"
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
        """Handle new token found - with liquidity and velocity validation"""
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

            # ============================================================================
            # ACCURACY FIX: Use blockchain reads instead of WebSocket data
            # WebSocket reports inflated values - blockchain is source of truth
            # ============================================================================

            # Extract mint for blockchain read
            token_data_ws = token_data.get('data', token_data) if 'data' in token_data else token_data
            mint = token_data['mint']

            # SPEED OPTIMIZATION: Skip blockchain for brand new tokens (always fails anyway)
            token_age = token_data.get('age', 0) or token_data.get('token_age', 0) or 0
            if token_age < 10:  # Skip blockchain for tokens < 10s old
                # Go straight to WebSocket adjustment (saves 156ms)
                logger.info(f"⚡ Skipping blockchain for new token (age: {token_age:.1f}s), using adjusted WebSocket")

                ws_sol = float(token_data_ws.get('vSolInBondingCurve', 0))
                ws_tokens = float(token_data_ws.get('vTokensInBondingCurve', 800_000_000))

                if ws_sol < 0.1:
                    logger.warning(f"❌ No valid data from WebSocket, skipping token")
                    return

                actual_sol = ws_sol * 0.625
                market_cap = actual_sol * 250

                logger.info(f"📊 WebSocket adjustment: {ws_sol:.2f} SOL → {actual_sol:.2f} SOL (62.5% factor)")
                logger.info(f"📊 Adjusted MC: ${market_cap:,.0f}")

                # Hardcode decimals for PumpFun (saves 126ms RPC call)
                token_decimals = 6  # PumpFun ALWAYS uses 6 decimals

                actual_tokens_atomic = int(ws_tokens * (10 ** token_decimals))
                actual_sol_lamports = int(actual_sol * 1e9)
                price_lamports_per_atomic = (actual_sol_lamports / actual_tokens_atomic) if actual_tokens_atomic > 0 else 0
                source_type = 'websocket_adjusted'
            else:
                # Only try blockchain for older tokens
                logger.info(f"📊 Reading blockchain for mature token (age: {token_age:.1f}s)")
                curve_state = self.curve_reader.get_curve_state(mint, use_cache=False)

                if not curve_state or not curve_state.get('is_valid'):
                    # Fallback for older tokens if blockchain fails
                    logger.warning(f"⚠️ Blockchain read failed, using adjusted WebSocket")
                    ws_sol = float(token_data_ws.get('vSolInBondingCurve', 0))
                    ws_tokens = float(token_data_ws.get('vTokensInBondingCurve', 800_000_000))

                    if ws_sol < 0.1:
                        logger.warning(f"❌ No valid data available")
                        return

                    actual_sol = ws_sol * 0.625
                    market_cap = actual_sol * 250
                    token_decimals = 6
                    actual_tokens_atomic = int(ws_tokens * (10 ** token_decimals))
                    actual_sol_lamports = int(actual_sol * 1e9)
                    price_lamports_per_atomic = (actual_sol_lamports / actual_tokens_atomic) if actual_tokens_atomic > 0 else 0
                    source_type = 'websocket_adjusted'
                else:
                    actual_sol = curve_state['sol_raised']
                    actual_tokens_atomic = curve_state['virtual_token_reserves']
                    actual_sol_lamports = curve_state['virtual_sol_reserves']
                    price_lamports_per_atomic = curve_state['price_lamports_per_atomic']
                    market_cap = actual_sol * 250
                    token_decimals = 6  # Hardcode for PumpFun
                    source_type = 'blockchain'
                    logger.info(f"✅ Blockchain data: {actual_sol:.4f} SOL, MC: ${market_cap:,.0f}")

            # Liquidity validation using REAL data
            required_sol = BUY_AMOUNT_SOL * LIQUIDITY_MULTIPLIER

            if actual_sol < MIN_LIQUIDITY_SOL:
                logger.warning(f"❌ Liquidity too low: {actual_sol:.4f} SOL < {MIN_LIQUIDITY_SOL} minimum")
                return

            if actual_sol < required_sol:
                logger.warning(f"❌ Insufficient liquidity: {actual_sol:.4f} SOL < {required_sol:.4f} ({LIQUIDITY_MULTIPLIER}x)")
                return

            logger.info(f"✅ Liquidity OK (Blockchain): {actual_sol:.4f} SOL (>= {LIQUIDITY_MULTIPLIER}x {BUY_AMOUNT_SOL})")

            # Build curve_data from blockchain (not WebSocket)
            curve_data = {
                'sol_raised': actual_sol,
                'sol_in_curve': actual_sol,
                'virtual_sol_reserves': actual_sol_lamports,
                'virtual_token_reserves': actual_tokens_atomic,
                'price_lamports_per_atomic': price_lamports_per_atomic,
                'source': source_type,  # Use the source_type variable from above
                'is_valid': True,
                'is_migrated': False
            }

            estimated_slippage = self.curve_reader.estimate_slippage(mint, BUY_AMOUNT_SOL)

            logger.info(f"⚡ Using blockchain data: {actual_sol:.4f} SOL, price={price_lamports_per_atomic:.10f} lamports/atom")
            logger.debug(f"✅ Curve data built from blockchain (accurate)")

            token_age = None
            
            if 'data' in token_data and 'age' in token_data['data']:
                token_age = token_data['data']['age']
                logger.debug(f"📊 Age from token_data.data.age: {token_age:.1f}s")
            elif 'age' in token_data:
                token_age = token_data['age']
                logger.debug(f"📊 Age from token_data.age: {token_age:.1f}s")
            elif 'token_age' in token_data:
                token_age = token_data['token_age']
                logger.debug(f"📊 Age from token_data.token_age: {token_age:.1f}s")
            
            if token_age is None or token_age < 0.5:  # Minimum 0.5s to avoid division issues
                sol_raised = curve_data.get('sol_raised', 0)

                if sol_raised > 0:
                    # Estimate age using realistic velocity assumptions
                    # Organic pumps: 2-4 SOL/s average
                    # Bot pumps: 8-15 SOL/s average
                    # Use 3.5 SOL/s as baseline (middle ground)
                    estimated_age = sol_raised / 3.5

                    # Clamp between 1.0s minimum and 15s maximum
                    # (tokens younger than 1s are unrealistic due to network/detection delays)
                    token_age = max(1.0, min(estimated_age, 15.0))

                    logger.info(
                        f"📊 Estimated age from SOL raised: {sol_raised:.2f} SOL ÷ 3.5 SOL/s "
                        f"= {token_age:.1f}s (clamped 1.0-15.0s)"
                    )
                else:
                    # Default to 2.5s if no curve data
                    token_age = 2.5
                    logger.warning(f"⚠️ No SOL raised data, using default age: {token_age:.1f}s")
            
            logger.info(f"📊 Using token age: {token_age:.1f}s for velocity check")
            logger.info(f"📊 SOL raised (from curve): {curve_data.get('sol_raised', 0):.4f}")
            logger.info(f"📊 Expected velocity: {curve_data.get('sol_raised', 0) / token_age:.2f} SOL/s")
            
            velocity_passed, velocity_reason = self.velocity_checker.check_velocity(
                mint=mint,
                curve_data=curve_data,
                token_age_seconds=token_age
            )
            
            if not velocity_passed:
                logger.warning(f"❌ Velocity check failed for {mint[:8]}...: {velocity_reason}")
                logger.info(f"   Calculated: {curve_data.get('sol_raised', 0) / token_age:.2f} SOL/s (need {VELOCITY_MIN_SOL_PER_SECOND})")
                return
            
            # ✅ Store the ESTIMATED entry price from detection (for comparison later)
            raw_entry_price = curve_data.get('price_lamports_per_atomic', 0)
            estimated_entry_price = raw_entry_price

            # ✅ FIX 1: token_decimals and estimated_slippage already fetched in parallel above
            if isinstance(token_decimals, tuple):
                token_decimals = token_decimals[0]
            if not token_decimals or token_decimals == 0:
                token_decimals = 6

            logger.debug(f"Estimated entry price (at detection): {estimated_entry_price:.10f} lamports/atomic")

            if estimated_slippage:
                logger.info(f"📊 Estimated slippage: {estimated_slippage:.2f}%")
                if estimated_slippage > MAX_SLIPPAGE_PERCENT:
                    logger.warning(f"⚠️ High estimated slippage ({estimated_slippage:.2f}% > {MAX_SLIPPAGE_PERCENT}%), skipping")
                    return
            
            self.pending_buys += 1
            logger.debug(f"Pending buys: {self.pending_buys}, Active: {len(self.positions)}")

            entry_market_cap = market_cap  # Use REAL blockchain-based market cap
            
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, token_data.get('source', 'pumpportal'), detection_time_ms)
            
            logger.info(f"🎯 Processing new token: {mint}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            logger.info(f"   Token age: {token_age:.1f}s")
            logger.info(f"   Estimated entry price: {estimated_entry_price:.10f} lamports/atomic")
            logger.info(f"   SOL raised: {curve_data['sol_raised']:.4f}")
            logger.info(f"   Velocity: {curve_data['sol_raised'] / token_age:.2f} SOL/s ✅")
            
            cost_breakdown = self.tracker.log_buy_attempt(mint, BUY_AMOUNT_SOL, 50)
            
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
            
            bought_tokens = 0
            actual_sol_spent = BUY_AMOUNT_SOL
            actual_entry_price = estimated_entry_price  # Will be updated if we get real data
            
            if signature:
                await asyncio.sleep(1.5)  # Reduced from 3s to 1.5s for faster confirmation
                
                txd = await self._get_transaction_deltas(signature, mint)
                
                # ✅ CRITICAL FIX: Always read actual wallet balance
                actual_wallet_balance = self.wallet.get_token_balance(mint)
                
                if txd["confirmed"] and txd["token_delta"] > 0:
                    bought_tokens = txd["token_delta"]
                    actual_sol_spent = abs(txd["sol_delta"])
                    
                    # ✅ Calculate actual entry price from transaction
                    token_decimals = self.wallet.get_token_decimals(mint)
                    if isinstance(token_decimals, tuple):
                        token_decimals = token_decimals[0]
                    if not token_decimals or token_decimals == 0:
                        token_decimals = 6
                    
                    token_atoms = int(bought_tokens * (10 ** token_decimals))
                    lamports_spent = int(actual_sol_spent * 1e9)
                    actual_entry_price = lamports_spent / token_atoms
                    
                    logger.info(f"✅ Real fill from TX: {bought_tokens:,.0f} tokens for {actual_sol_spent:.6f} SOL")
                    logger.debug(f"Actual entry price from TX: {actual_entry_price:.10f} lamports/atomic")
                    
                    # Verify against wallet balance
                    if actual_wallet_balance > 0 and abs(actual_wallet_balance - bought_tokens) > (bought_tokens * 0.1):
                        logger.warning(f"⚠️ Wallet balance mismatch! TX says {bought_tokens:,.0f} but wallet has {actual_wallet_balance:,.0f}")
                        bought_tokens = actual_wallet_balance  # Use actual balance
                        
                        # Recalculate entry price with corrected token amount
                        token_atoms = int(bought_tokens * (10 ** token_decimals))
                        actual_entry_price = lamports_spent / token_atoms
                        logger.info(f"✅ Corrected entry price: {actual_entry_price:.10f} lamports/atomic")
                        
                else:
                    # ✅ CRITICAL FIX #1: Transaction reading failed - calculate from wallet balance
                    if actual_wallet_balance > 0:
                        bought_tokens = actual_wallet_balance
                        actual_sol_spent = BUY_AMOUNT_SOL
                        logger.warning(f"⚠️ TX reading failed - using wallet balance: {bought_tokens:,.0f} tokens")
                        
                        # ✅ RECALCULATE entry price from actual fill
                        token_decimals = self.wallet.get_token_decimals(mint)
                        if isinstance(token_decimals, tuple):
                            token_decimals = token_decimals[0]
                        if not token_decimals or token_decimals == 0:
                            token_decimals = 6
                        
                        token_atoms = int(bought_tokens * (10 ** token_decimals))
                        lamports_spent = int(actual_sol_spent * 1e9)
                        actual_entry_price = lamports_spent / token_atoms
                        
                        logger.info(f"✅ Recalculated entry price from fill: {actual_entry_price:.10f} lamports/atomic")
                        logger.info(f"   Calculation: {actual_sol_spent:.6f} SOL ({lamports_spent:,} lamports) / {bought_tokens:,.0f} tokens ({token_atoms:,} atoms)")
                        
                    else:
                        # ✅ CRITICAL FIX #2: Last resort - wait longer and check again
                        logger.warning("⚠️ No tokens in wallet immediately - waiting 2s more")
                        await asyncio.sleep(2)
                        bought_tokens = self.wallet.get_token_balance(mint)
                        actual_sol_spent = BUY_AMOUNT_SOL
                        
                        if bought_tokens == 0:
                            logger.error("❌ Still no tokens - position may have failed")
                            self.pending_buys -= 1
                            return
                        
                        # ✅ RECALCULATE entry price after retry
                        token_decimals = self.wallet.get_token_decimals(mint)
                        if isinstance(token_decimals, tuple):
                            token_decimals = token_decimals[0]
                        if not token_decimals or token_decimals == 0:
                            token_decimals = 6
                        
                        token_atoms = int(bought_tokens * (10 ** token_decimals))
                        lamports_spent = int(actual_sol_spent * 1e9)
                        actual_entry_price = lamports_spent / token_atoms
                        
                        logger.info(f"✅ Recalculated entry price after retry: {actual_entry_price:.10f} lamports/atomic")
                
                # ✅ CHATGPT FIX: Log estimated vs actual price comparison
                if bought_tokens > 0:
                    price_diff_pct = abs(actual_entry_price - estimated_entry_price) / estimated_entry_price * 100 if estimated_entry_price > 0 else 0

                    logger.info(f"📊 Entry Price Verification:")
                    logger.info(f"   Estimated (at detection): {estimated_entry_price:.10f} lamports/atom")
                    logger.info(f"   Actual (from fill): {actual_entry_price:.10f} lamports/atom")
                    logger.info(f"   Difference: {price_diff_pct:.1f}%")

                    if price_diff_pct > 10:
                        logger.warning(f"⚠️ LARGE ENTRY PRICE DISCREPANCY: {price_diff_pct:.1f}%")
                        logger.warning(f"   Price moved significantly between detection and execution")

                    # 🚨 SLIPPAGE PROTECTION - Exit immediately if slippage too high
                    from config import MAX_ENTRY_SLIPPAGE_PERCENT

                    # Define variables for logging (prevent UnboundLocalError)
                    token_data_inner = token_data.get('data', token_data) if 'data' in token_data else token_data
                    creator_sol_amount = float(token_data_inner.get('solAmount', 1))
                    sol_in_curve_amount = actual_sol  # Use the actual_sol from earlier
                    momentum_value = sol_in_curve_amount / creator_sol_amount if creator_sol_amount > 0 else 0

                    if price_diff_pct > MAX_ENTRY_SLIPPAGE_PERCENT:
                        logger.error(f"🚨 EXCESSIVE SLIPPAGE DETECTED: {price_diff_pct:.1f}% > {MAX_ENTRY_SLIPPAGE_PERCENT}% threshold")
                        logger.error(f"   This indicates you bought during a price spike (bot swarm)")
                        expected_tokens = (actual_sol_spent * 1e9) / estimated_entry_price if estimated_entry_price > 0 else 0
                        token_shortfall = ((expected_tokens - bought_tokens) / expected_tokens * 100) if expected_tokens > 0 else 0
                        logger.error(f"   Expected: {expected_tokens:,.0f} tokens")
                        logger.error(f"   Received: {bought_tokens:,.0f} tokens ({token_shortfall:.1f}% fewer)")
                        logger.error(f"   Exiting immediately to limit damage...")

                        # ✅ FIX: Calculate execution time before logging
                        execution_time_ms = (time.time() - execution_start) * 1000

                        # Record the buy first (so we have a position to close)
                        self.tracker.log_buy_executed(
                            mint=mint,
                            amount_sol=actual_sol_spent,
                            signature=signature,
                            tokens_received=bought_tokens,
                            execution_time_ms=execution_time_ms,
                            age_at_detection=token_data.get('age', 0),
                            age_at_buy=token_age,
                            sol_in_curve=sol_in_curve_amount,
                            creator_sol=creator_sol_amount,
                            momentum=momentum_value,
                            mc_at_entry=entry_market_cap,
                            entry_price=actual_entry_price
                        )

                        # Create position object
                        position = Position(
                            mint=mint,
                            sol_amount=actual_sol_spent,
                            token_amount=bought_tokens,
                            entry_market_cap=entry_market_cap
                        )
                        position.buy_signature = signature
                        position.entry_time = time.time()
                        position.entry_token_price_sol = actual_entry_price
                        position.entry_sol_in_curve = curve_data.get('sol_raised', 0.0) if curve_data else 0.0

                        # Add to positions dict temporarily
                        self.positions[mint] = position
                        self.pending_buys -= 1

                        # Wait 0.5s for transaction to settle, then sell immediately
                        await asyncio.sleep(0.5)
                        await self._close_position_full(mint, reason="high_slippage")
                        return
            
            if signature and bought_tokens > 0:
                execution_time_ms = (time.time() - execution_start) * 1000

                # Calculate momentum for logging
                token_data_inner = token_data.get('data', token_data) if 'data' in token_data else token_data
                creator_sol_amount = token_data_inner.get('solAmount', 1)
                sol_in_curve_amount = curve_data.get('sol_raised', 0)
                momentum_value = sol_in_curve_amount / creator_sol_amount if creator_sol_amount > 0 else 0

                self.tracker.log_buy_executed(
                    mint=mint,
                    amount_sol=actual_sol_spent,
                    signature=signature,
                    tokens_received=bought_tokens,
                    execution_time_ms=execution_time_ms,
                    age_at_detection=token_data.get('age', 0),
                    age_at_buy=token_age,
                    sol_in_curve=sol_in_curve_amount,
                    creator_sol=creator_sol_amount,
                    momentum=momentum_value,
                    mc_at_entry=entry_market_cap,
                    entry_price=actual_entry_price
                )
                
                position = Position(mint, actual_sol_spent, bought_tokens, entry_market_cap)
                position.buy_signature = signature
                position.initial_tokens = bought_tokens
                position.remaining_tokens = bought_tokens
                position.last_valid_balance = bought_tokens
                position.entry_time = time.time()
                position.entry_token_price_sol = actual_entry_price  # ✅ Use ACTUAL entry price
                position.amount_sol = actual_sol_spent
                
                variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                
                if 'data' in token_data:
                    position.entry_sol_in_curve = token_data['data'].get('vSolInBondingCurve', 30)
                
                self.positions[mint] = position
                self.total_trades += 1
                self.pending_buys -= 1
                
                exit_in_seconds = position.exit_time - position.entry_time
                
                logger.info(f"✅ BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Amount: {actual_sol_spent:.6f} SOL")
                logger.info(f"   Tokens: {bought_tokens:,.0f}")
                logger.info(f"   Actual Entry Price: {actual_entry_price:.10f} lamports/atomic")
                logger.info(f"   ⏱️ Exit timer: {exit_in_seconds:.1f}s")
                logger.info(f"   ⚠️ Fail-fast check at: {FAIL_FAST_CHECK_TIME}s")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                if self.telegram:
                    await self.telegram.notify_buy(mint, actual_sol_spent, signature)
                
                # ✅ CHATGPT FIX #5: Seed post-buy chain price to avoid stale WebSocket data
                await asyncio.sleep(0.8)
                seed = self.dex.get_bonding_curve_data(mint, prefer_chain=True)
                if seed and seed.get('source') == 'chain':
                    logger.info("🔎 Seeded post-buy price from [chain]")
                else:
                    logger.info("🔎 Could not seed chain price; monitor will require chain before SL/rug")
                
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                logger.info(f"📊 Started monitoring position {mint[:8]}...")
            else:
                self.pending_buys -= 1
                self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, "Transaction failed or no tokens received")
                
        except Exception as e:
            self.pending_buys = max(0, self.pending_buys - 1)
            logger.error(f"Failed to process token: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, str(e))
    
    async def _monitor_position(self, mint: str):
        """Monitor position - MOMENTUM-BASED EXITS"""
        try:
            position = self.positions.get(mint)
            if not position:
                return

            # ✅ ADD THESE NEW VARIABLES
            max_pnl_reached = 0  # Track peak P&L for drawdown detection
            last_sol_raised = 0  # Track SOL flow for velocity
            last_velocity_check = time.time()

            logger.info(f"📈 Starting MOMENTUM monitoring for {mint[:8]}...")
            logger.info(f"   Entry Price: {position.entry_token_price_sol:.10f} lamports/atomic")
            logger.info(f"   Exit Time: {position.exit_time - position.entry_time:.1f}s from now")
            logger.info(f"   Fail-fast check: {FAIL_FAST_CHECK_TIME}s")
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
                    # ✅ CHATGPT FIX: Prefer chain until we have first chain price
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
                    
                    # ✅ CHATGPT FIX #6: Track price source
                    source = curve_data.get('source', 'unknown')
                    position.last_price_source = source
                    if source == 'chain':
                        position.has_chain_price = True
                    
                    if curve_data.get('is_migrated'):
                        logger.warning(f"❌ Token {mint[:8]}... has migrated - exiting immediately")
                        await self._close_position_full(mint, reason="migration")
                        break
                    
                    if curve_data.get('sol_in_curve', 0) <= 0:
                        consecutive_data_failures += 1
                        logger.warning(f"Invalid SOL in curve data (failure {consecutive_data_failures}/{DATA_FAILURE_TOLERANCE})")
                        await asyncio.sleep(1)
                        continue
                    
                    current_sol_in_curve = curve_data.get('sol_in_curve', 0)
                    
                    current_token_price_sol = self._get_current_token_price(mint, curve_data)
                    
                    if current_token_price_sol is None:
                        consecutive_data_failures += 1
                        logger.warning(f"Could not calculate price (failure {consecutive_data_failures}/{DATA_FAILURE_TOLERANCE})")
                        await asyncio.sleep(1)
                        continue
                    
                    if current_token_price_sol <= 0:
                        logger.warning(f"Invalid price calculated: {current_token_price_sol}")
                        consecutive_data_failures += 1
                        await asyncio.sleep(1)
                        continue
                    
                    if position.entry_token_price_sol > 0:
                        price_change = ((current_token_price_sol / position.entry_token_price_sol) - 1) * 100
                    else:
                        price_change = 0
                    
                    position.pnl_percent = price_change
                    position.current_price = current_token_price_sol
                    position.max_pnl_reached = max(position.max_pnl_reached, price_change)

                    # ✅ ADD: Update local tracking
                    max_pnl_reached = max(max_pnl_reached, price_change)

                    consecutive_data_failures = 0
                    position.last_valid_price = current_token_price_sol
                    position.last_price_update = time.time()
                    
                    if check_count == 1:
                        logger.info(f"📊 First price check for {mint[:8]}...")
                        logger.info(f"   Entry: {position.entry_token_price_sol:.10f} lamports/atomic")
                        logger.info(f"   Current: {current_token_price_sol:.10f} lamports/atomic")
                        logger.info(f"   P&L: {price_change:+.1f}%")
                    
                    self.velocity_checker.update_snapshot(
                        mint, 
                        current_sol_in_curve, 
                        int(current_sol_in_curve / 0.4)
                    )
                    
                    # ✅ CHATGPT FIX: Fast Profit Protectors (run BEFORE fail-fast, stop-loss, timer)
                    # Only trust chain ticks for profit-based exits (same as stop-loss gating)
                    on_chain = (source == 'chain') and position.has_chain_price
                    
                    # 1) EXTREME TAKE-PROFIT (catches parabolic pumps)
                    if on_chain and not position.is_closing:
                        if price_change >= EXTREME_TP_PERCENT:
                            logger.info(
                                f"💰 EXTREME TAKE PROFIT on [chain]: {price_change:+.1f}% "
                                f"(threshold: {EXTREME_TP_PERCENT}%, peak: {position.max_pnl_reached:+.1f}%)"
                            )
                            await self._close_position_full(mint, reason="extreme_take_profit")
                            break
                    
                    # 2) TRAILING STOP (protects profits from fast rugs)
                    if on_chain and not position.is_closing:
                        if position.max_pnl_reached >= TRAIL_START_PERCENT:
                            drop_from_peak = position.max_pnl_reached - price_change
                            if drop_from_peak >= TRAIL_GIVEBACK_PERCENT:
                                logger.warning(
                                    f"📉 TRAILING STOP on [chain]: drop {drop_from_peak:.1f}pp "
                                    f"from +{position.max_pnl_reached:.1f}% peak → current {price_change:+.1f}%"
                                )
                                await self._close_position_full(mint, reason="trailing_stop")
                                break
                    
                    if (age >= FAIL_FAST_CHECK_TIME and 
                        not position.fail_fast_checked and 
                        not position.is_closing):

                        # ✅ CHATGPT FIX: Only evaluate & finalize fail-fast on [chain] tick
                        if not position.has_chain_price or source != 'chain':
                            logger.warning(
                                f"🚧 FAIL-FAST from [{source}] ignored until first [chain] tick"
                            )
                        else:
                            # Commit the one-time fail-fast check now that we have chain data
                            position.fail_fast_checked = True

                            # PNL branch (gated to chain)
                            if price_change < FAIL_FAST_PNL_THRESHOLD:
                                logger.warning(
                                    f"⚠️ FAIL-FAST: P&L {price_change:.1f}% < {FAIL_FAST_PNL_THRESHOLD}% at {age:.1f}s "
                                    f"(on [chain]) - exiting immediately"
                                )
                                await self._close_position_full(mint, reason="fail_fast_pnl")
                                break

                            # Velocity branch (also on chain)
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
                            else:
                                logger.info(
                                    f"✅ FAIL-FAST CHECK PASSED at {age:.1f}s: "
                                    f"P&L {price_change:+.1f}%"
                                )
                    
                    # ✅ CHATGPT FIX #7: Only allow rug trap on chain price
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

                    # ============================================
                    # NEW EXIT SIGNAL 1: Peak Drawdown (Momentum Death)
                    # ============================================
                    if max_pnl_reached > MOMENTUM_MIN_PEAK_PERCENT and not position.is_closing:
                        drawdown_from_peak = max_pnl_reached - price_change

                        if drawdown_from_peak >= MOMENTUM_MAX_DRAWDOWN_PP:
                            logger.warning(
                                f"💨 MOMENTUM DEATH: Peak {max_pnl_reached:+.1f}% → Now {price_change:+.1f}% "
                                f"(drawdown: {drawdown_from_peak:.1f}pp, threshold: {MOMENTUM_MAX_DRAWDOWN_PP:.1f}pp)"
                            )
                            await self._close_position_full(mint, reason="momentum_death")
                            break

                    # ============================================
                    # NEW EXIT SIGNAL 2: Velocity Death Check
                    # ============================================
                    if age >= 5.0 and age < 6.0 and not position.fail_fast_checked:  # Check once at 5s
                        position.fail_fast_checked = True

                        pre_buy_velocity = self.velocity_checker.get_pre_buy_velocity(mint)
                        if pre_buy_velocity:
                            current_velocity = current_sol_in_curve / max(age, 0.1)
                            velocity_ratio = (current_velocity / pre_buy_velocity) * 100

                            if velocity_ratio < MOMENTUM_VELOCITY_DEATH_PERCENT:
                                logger.warning(
                                    f"💨 VELOCITY DIED: {current_velocity:.2f} SOL/s = {velocity_ratio:.0f}% "
                                    f"of entry ({pre_buy_velocity:.2f} SOL/s), threshold: {MOMENTUM_VELOCITY_DEATH_PERCENT:.0f}%"
                                )
                                await self._close_position_full(mint, reason="velocity_death")
                                break

                    # ============================================
                    # NEW EXIT SIGNAL 3: Big Win Take Profit
                    # ============================================
                    if price_change >= MOMENTUM_BIG_WIN_PERCENT and not position.is_closing:
                        logger.info(
                            f"💰 BIG WIN TAKE PROFIT: {price_change:+.1f}% "
                            f"(peak: {max_pnl_reached:+.1f}%, threshold: {MOMENTUM_BIG_WIN_PERCENT:.1f}%)"
                        )
                        await self._close_position_full(mint, reason="big_win_tp")
                        break

                    # ============================================
                    # NEW EXIT SIGNAL 4: Max Hold Time Backstop
                    # ============================================
                    if age > MOMENTUM_MAX_HOLD_SECONDS and not position.is_closing:
                        logger.warning(
                            f"⏰ MAX HOLD TIME: {age:.1f}s > {MOMENTUM_MAX_HOLD_SECONDS:.0f}s limit "
                            f"(P&L: {price_change:+.1f}%, peak: {max_pnl_reached:+.1f}%)"
                        )
                        await self._close_position_full(mint, reason="max_hold_time")
                        break

                    # ============================================
                    # KEEP EXISTING: Timer exit (but will rarely trigger now)
                    # ============================================
                    if time_until_exit <= 0 and not position.is_closing:
                        logger.info(f"⏰ TIMER EXPIRED for {mint[:8]}... - exiting")
                        logger.info(f"   Final P&L: {price_change:+.1f}%")
                        logger.info(f"   Max P&L reached: {position.max_pnl_reached:+.1f}%")
                        await self._close_position_full(mint, reason="timer_exit")
                        break
                    
                    if (time_until_exit <= 5 and
                        time_until_exit > 0 and
                        price_change > TIMER_EXTENSION_PNL_THRESHOLD and
                        position.extensions_used < TIMER_MAX_EXTENSIONS and
                        not position.is_closing):
                        
                        is_accelerating = self.velocity_checker.is_velocity_accelerating(
                            mint, 
                            current_sol_in_curve
                        )
                        
                        if is_accelerating:
                            position.exit_time += TIMER_EXTENSION_SECONDS
                            position.extensions_used += 1
                            logger.info(
                                f"🚀 EXTENDING TIMER for {mint[:8]}... "
                                f"(+{TIMER_EXTENSION_SECONDS}s, extension {position.extensions_used}/{TIMER_MAX_EXTENSIONS})"
                            )
                            logger.info(f"   P&L: {price_change:+.1f}%, velocity accelerating")
                    
                    if check_count % 3 == 1:
                        logger.info(
                            f"⏱️ {mint[:8]}... | P&L: {price_change:+.1f}% | "
                            f"Exit in: {time_until_exit:.1f}s | "
                            f"Extensions: {position.extensions_used}/{TIMER_MAX_EXTENSIONS}"
                        )
                    
                    # ✅ CRITICAL FIX: Stop loss now requires [chain] source like rug trap
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
        """Execute partial sell with priority fees (LEGACY - kept for compatibility)"""
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
        """Track ACTUAL SOL received from wallet balance changes (LEGACY)"""
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
        """
        Close remaining position with ROBUST confirmation (FIXED)
        ✅ Now uses same 25-second polling as partial sells
        """
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

            actual_balance = self.wallet.get_token_balance(mint)
            if actual_balance > 0:
                ui_token_balance = actual_balance
                logger.info(f"💰 Selling actual balance: {actual_balance:,.2f} tokens")
            else:
                logger.warning(f"No tokens in wallet - using position balance: {ui_token_balance:,.2f}")
            
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
                
                if reason in ["migration", "max_age", "no_data"]:
                    logger.warning(f"Removing unsellable position {mint[:8]}...")
                    if self.telegram:
                        await self.telegram.send_message(
                            f"⚠️ Could not sell {mint[:16]}\n"
                            f"Reason: {reason}\nRemoving to free slot"
                        )
                
                if mint in self.positions:
                    del self.positions[mint]
                    self.velocity_checker.clear_history(mint)
                return

            # ✅ ROBUST: Parse transaction directly (NO wallet balance delta!)
            logger.info(f"⏳ Parsing transaction proceeds from blockchain...")
            logger.info(f"🔗 Solscan: https://solscan.io/tx/{signature}")

            tx_result = await self._get_transaction_proceeds_robust(signature, mint, max_wait=30)

            if tx_result["success"]:
                # Got EXACT proceeds from transaction
                actual_sol_received = tx_result["sol_received"]
                actual_tokens_sold = tx_result["tokens_sold"]

                logger.info(f"✅ Transaction parsing successful:")
                logger.info(f"   Wait time: {tx_result['wait_time']:.1f}s")
                logger.info(f"   SOL received: {actual_sol_received:+.6f} SOL")
                logger.info(f"   Tokens sold: {actual_tokens_sold:,.2f}")

                # Calculate accurate P&L
                estimated_fees = 0.009
                trading_pnl_sol = actual_sol_received - position.amount_sol

                logger.info(f"📊 P&L Calculation:")
                logger.info(f"   SOL received: {actual_sol_received:.6f}")
                logger.info(f"   SOL invested: {position.amount_sol:.6f}")
                logger.info(f"   Trading P&L: {trading_pnl_sol:+.6f} SOL")
                logger.info(f"   Estimated fees: {estimated_fees:.6f} SOL")

                gross_sale_proceeds = actual_sol_received
                actual_fees_paid = estimated_fees
                final_pnl_sol = trading_pnl_sol

            else:
                # Transaction parsing failed - use conservative fallback
                logger.warning("⚠️ Transaction parsing failed after 30s")
                logger.warning("   Using conservative estimate - check Solscan manually!")
                logger.warning(f"   Transaction: https://solscan.io/tx/{signature}")

                # Conservative estimate: assume small loss equal to fees
                estimated_fees = 0.009
                trading_pnl_sol = -estimated_fees
                actual_sol_received = position.amount_sol - estimated_fees
                actual_tokens_sold = ui_token_balance
                actual_fees_paid = estimated_fees

                gross_sale_proceeds = actual_sol_received
                final_pnl_sol = trading_pnl_sol

            # Detect suspicious/failed sells (check actual sale proceeds)
            if gross_sale_proceeds > 0 and gross_sale_proceeds < (position.amount_sol * 0.1):
                logger.error(f"⚠️ SUSPICIOUS SELL: Only got {gross_sale_proceeds:.6f} SOL from sale (invested {position.amount_sol} SOL)")
                logger.error(f"   This suggests curve was dead/migrated during sell")
                logger.error(f"   Transaction: https://solscan.io/tx/{signature}")
            elif final_pnl_sol < 0:
                logger.debug(f"Normal loss: Trading P&L {final_pnl_sol:+.6f} SOL (fees: {actual_fees_paid:.6f} SOL)")
            
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
                tokens_sold=actual_tokens_sold,
                signature=signature,
                sol_received=gross_sale_proceeds,  # ✅ Gross sale proceeds (corrected timing)
                pnl_sol=final_pnl_sol,  # ✅ Pure trading P&L
                fees_paid=actual_fees_paid,  # ✅ Separate fees
                pnl_percent=position.pnl_percent,
                hold_time_seconds=hold_time,
                reason=reason,
                max_pnl_reached=position.max_pnl_reached,
                exit_price=position.current_price,
                mc_at_exit=getattr(position, 'current_market_cap', 0)
            )

            # Calculate net realized (includes fees)
            net_realized_sol = final_pnl_sol - actual_fees_paid

            logger.info(f"✅ POSITION CLOSED: {mint[:8]}...")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Hold time: {hold_time:.1f}s")
            logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
            logger.info(f"   Trading P&L: {final_pnl_sol:+.4f} SOL")
            logger.info(f"   Fees Paid: {actual_fees_paid:.4f} SOL")
            logger.info(f"   Net Realized: {net_realized_sol:+.4f} SOL")
            logger.info(f"   Consecutive losses: {self.consecutive_losses}")
            
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
        """Main run loop"""
        self.running = True
        
        try:
            await self.initialize_telegram()
            
            self.scanner = PumpPortalMonitor(self.on_token_found)
            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Bot running with VELOCITY + TIMER + FAIL-FAST STRATEGY")
            logger.info(f"⚡ Velocity: ≥{VELOCITY_MIN_SOL_PER_SECOND} SOL/s, ≥{VELOCITY_MIN_BUYERS} buyers")
            logger.info(f"⚡ Recent: ≥{VELOCITY_MIN_RECENT_1S_SOL} SOL (1s), ≥{VELOCITY_MIN_RECENT_3S_SOL} SOL (3s)")
            logger.info(f"⏱️ Timer: {TIMER_EXIT_BASE_SECONDS}s ±{TIMER_EXIT_VARIANCE_SECONDS}s")
            logger.info(f"⚠️ Fail-fast: {FAIL_FAST_CHECK_TIME}s @ {FAIL_FAST_PNL_THRESHOLD}%")
            logger.info(f"🎯 Circuit breaker: 3 consecutive losses")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            age = time.time() - pos.entry_time
                            time_until_exit = pos.exit_time - time.time()
                            logger.info(
                                f"  • {mint[:8]}... | P&L: {pos.pnl_percent:+.1f}% | "
                                f"Max: {pos.max_pnl_reached:+.1f}% | "
                                f"Exit in: {time_until_exit:.1f}s | "
                                f"Extensions: {pos.extensions_used}/{TIMER_MAX_EXTENSIONS}"
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
                
                if self.scanner_task and self.scanner_task.done():
                    if not self.shutdown_requested:
                        exc = self.scanner_task.exception()
                        if exc:
                            logger.error(f"Scanner died: {exc}")
                            logger.info("Restarting scanner...")
                            self.scanner_task = asyncio.create_task(self.scanner.start())
            
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
