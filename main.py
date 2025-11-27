"""
Main Orchestrator - FINAL FIX: Entry price bookkeeping + Stop loss with source checking
"""

import asyncio
import logging
import signal
import time
import random
import os
from datetime import datetime
from typing import Dict, Optional, List

from config import (
    LOG_LEVEL, LOG_FORMAT, RPC_ENDPOINT,
    BUY_AMOUNT_SOL, MAX_POSITIONS, MIN_SOL_BALANCE,
    STOP_LOSS_PERCENTAGE, TAKE_PROFIT_PERCENTAGE,
    SELL_DELAY_SECONDS, MAX_POSITION_AGE_SECONDS,
    MONITOR_CHECK_INTERVAL, DATA_FAILURE_TOLERANCE,
    DRY_RUN, ENABLE_TELEGRAM_NOTIFICATIONS,
    BLACKLISTED_TOKENS, NOTIFY_PROFIT_THRESHOLD,
    PARTIAL_TAKE_PROFIT, LIQUIDITY_MULTIPLIER,
    MIN_LIQUIDITY_SOL, MAX_SLIPPAGE_PERCENT,
    VELOCITY_MIN_SOL_PER_SECOND, VELOCITY_MIN_BUYERS, VELOCITY_MAX_TOKEN_AGE,
    VELOCITY_MIN_RECENT_1S_SOL, VELOCITY_MIN_RECENT_3S_SOL, VELOCITY_MAX_DROP_PERCENT,
    # Velocity ceiling parameters
    VELOCITY_MAX_SOL_PER_SECOND, VELOCITY_MAX_RECENT_1S_SOL, VELOCITY_MAX_RECENT_3S_SOL,
    # Tiered take-profit (whale strategy)
    TIER_1_PROFIT_PERCENT, TIER_1_SELL_PERCENT,
    TIER_2_PROFIT_PERCENT, TIER_2_SELL_PERCENT,
    TIER_3_PROFIT_PERCENT, TIER_3_SELL_PERCENT,
    # Timer exit parameters
    TIMER_EXIT_BASE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS,
    TIMER_MAX_EXTENSIONS,
    FAIL_FAST_CHECK_TIME, FAIL_FAST_PNL_THRESHOLD,
    MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL,
)

from wallet import WalletManager
from dex import PumpFunDEX
from helius_logs_monitor import HeliusLogsMonitor
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
        
        # ✅ FLATLINE DETECTION: Track when P&L last improved
        self.last_pnl_change_time = time.time()
        self.last_recorded_pnl = -999  # Start at impossible value
        self.first_price_check_done = False

class SniperBot:
    """Main sniper bot orchestrator with velocity gate, timer exits, and fail-fast"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING SNIPER BOT - WHALE TIERED EXITS")
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
            min_sol_per_second=2.0,              # Lowered from 2.5
            min_unique_buyers=VELOCITY_MIN_BUYERS,
            max_token_age_seconds=16.0,          # Raised from 15.0
            min_recent_1s_sol=2.0,               # Lowered from 2.5
            min_recent_3s_sol=4.0,               # Lowered from 5.0
            max_drop_percent=VELOCITY_MAX_DROP_PERCENT,
            min_snapshots=1,                     # ✅ CRITICAL: Already 1
            max_sol_per_second=15.0,             # Raised from 10.0
            max_recent_1s_sol=20.0,              # Raised from 15.0
            max_recent_3s_sol=35.0               # Raised from 25.0
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

        # Initialize SOL price cache
        self._sol_price_cache = {
            'price': None,
            'timestamp': 0
        }

        self._log_startup_info()
    
    def _log_startup_info(self):
        """Log startup information"""
        sol_balance = self.wallet.get_sol_balance()
        tradeable_balance = max(0, sol_balance - MIN_SOL_BALANCE)
        max_trades = int(tradeable_balance / BUY_AMOUNT_SOL) if tradeable_balance > 0 else 0
        actual_trades = min(max_trades, MAX_POSITIONS) if max_trades > 0 else 0
        
        logger.info(f"📊 STARTUP STATUS:")
        logger.info(f"  • Strategy: ⚡ HELIUS INSTANT DETECTION + WHALE EXITS")
        logger.info(f"  • Detection: <100ms via logsSubscribe (no RPC delay)")
        logger.info(f"  • Entry range: 21-28 SOL (5-6.5K MC sweet spot)")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Take profit: {TIER_1_SELL_PERCENT}% @ +{TIER_1_PROFIT_PERCENT}%, {TIER_2_SELL_PERCENT}% @ +{TIER_2_PROFIT_PERCENT}%, {TIER_3_SELL_PERCENT}% @ +{TIER_3_PROFIT_PERCENT}%")
        logger.info(f"  • Max hold: {MAX_POSITION_AGE_SECONDS}s (let winners run)")
        logger.info(f"  • Velocity gate: 2.0-15.0 SOL/s avg, ≥{VELOCITY_MIN_BUYERS} buyers")
        logger.info(f"  • Liquidity gate: {LIQUIDITY_MULTIPLIER}x buy size (min {MIN_LIQUIDITY_SOL} SOL)")
        logger.info(f"  • Max slippage: {MAX_SLIPPAGE_PERCENT}%")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Circuit breaker: 3 consecutive losses")
        logger.info(f"  • Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
        logger.info("=" * 60)


    def _calculate_mc_from_curve(self, curve_data: dict, sol_price_usd: float) -> float:
        """Calculate market cap from bonding curve data"""
        try:
            # ✅ CORRECT: Get reserves and convert to human-readable
            v_sol_lamports = curve_data.get('virtual_sol_reserves', 0)
            v_tokens_atomic = curve_data.get('virtual_token_reserves', 0)

            if v_sol_lamports == 0 or v_tokens_atomic == 0:
                return 0

            # Convert to human units
            v_sol_human = v_sol_lamports / 1e9
            v_tokens_human = v_tokens_atomic / 1e6  # PumpFun uses 6 decimals

            # Calculate price per token in SOL
            price_per_token_sol = v_sol_human / v_tokens_human

            # Calculate market cap
            total_supply = 1_000_000_000
            market_cap_usd = total_supply * price_per_token_sol * sol_price_usd

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

    async def _fetch_sol_price_birdeye(self) -> float:
        """
        Fetch current SOL price from Birdeye with correct API format.
        Returns None if fetch fails.
        """
        api_key = os.getenv("BIRDEYE_API_KEY")
        if not api_key:
            logger.warning("⚠️ BIRDEYE_API_KEY not set in environment")
            return None

        url = "https://public-api.birdeye.so/public/price"
        params = {
            "address": "So11111111111111111111111111111111111111112",
            "chain": "solana",
        }
        headers = {
            "accept": "application/json",
            "x-api-key": api_key,
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(
                            "⚠️ Birdeye request failed (status=%s, body=%s)",
                            resp.status,
                            text[:200],
                        )
                        return None

                    data = await resp.json()

                    # Extract price from response
                    price = None
                    if isinstance(data, dict):
                        payload = data.get("data") or {}
                        if isinstance(payload, dict):
                            price = payload.get("value") or payload.get("price")

                    if price is None:
                        logger.warning("⚠️ Birdeye response missing price field")
                        return None

                    return float(price)

        except Exception as e:
            logger.error(f"⚠️ Exception fetching SOL price from Birdeye: {e}")
            return None

    async def _get_sol_price_async(self) -> float:
        """
        Get SOL price in USD, using Birdeye + 5 min cache.
        Falls back to APPROX_SOL_PRICE_USD if Birdeye fails.
        ALWAYS returns a valid float, NEVER None.
        """
        now = time.time()

        # Return cached price if recent and valid
        cached_price = self._sol_price_cache.get("price")
        cached_ts = self._sol_price_cache.get("timestamp", 0)

        if cached_price is not None and (now - cached_ts) < 300:
            return float(cached_price)

        # Try Birdeye
        new_price = await self._fetch_sol_price_birdeye()
        if new_price is not None:
            self._sol_price_cache["price"] = float(new_price)
            self._sol_price_cache["timestamp"] = now
            logger.info(f"💵 Updated SOL price from Birdeye: ${new_price:.2f}")
            return float(new_price)

        # Fallback - also set cache so it's never None
        from config import APPROX_SOL_PRICE_USD
        logger.warning(f"⚠️ Birdeye fetch failed, using fallback SOL=${APPROX_SOL_PRICE_USD:.2f}")
        self._sol_price_cache["price"] = float(APPROX_SOL_PRICE_USD)
        self._sol_price_cache["timestamp"] = now
        return float(APPROX_SOL_PRICE_USD)

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
                logger.warning(f"⏱️ Timeout: TX never appeared in RPC after {wait_time:.1f}s")
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
                    "🚀 Bot started - WHALE TIERED EXITS\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    f"⚡ Velocity: ≥{VELOCITY_MIN_SOL_PER_SECOND} SOL/s\n"
                    f"💰 Tiers: 40%@+30%, 40%@+60%, 20%@+100%\n"
                    f"🛑 Stop loss: -{STOP_LOSS_PERCENTAGE}%\n"
                    f"⏱️ Max hold: {MAX_POSITION_AGE_SECONDS}s\n"
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
            from solana.rpc.api import Client
            rpc_client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
            self.scanner = HeliusLogsMonitor(self.on_token_found, rpc_client)
        
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
            
            # Handle different data sources
            source = token_data.get('source', 'pumpportal')

            if source == 'helius_events':
                # Event-driven data - already validated by monitor
                event_data = token_data.get('data', {})

                actual_sol = event_data.get('vSolInBondingCurve', 0)
                unique_buyers = event_data.get('unique_buyers', 0)
                buy_count = event_data.get('buy_count', 0)
                sell_count = event_data.get('sell_count', 0)
                velocity = event_data.get('velocity', 0)
                token_age = token_data.get('age', 2.0)

                logger.info(f"⚡ HELIUS EVENT-DRIVEN ENTRY: {mint[:8]}...")
                logger.info(f"   SOL: {actual_sol:.2f} | Buyers: {unique_buyers} | Velocity: {velocity:.2f}/s")
                logger.info(f"   Buys: {buy_count} | Sells: {sell_count} | Age: {token_age:.1f}s")

                # Skip velocity checker - already validated by event monitor
                # Skip holder check - we have real buyer count
                # Skip SOL range check - already validated

                source_type = 'helius_events'

            elif source == 'helius_logs':
                # Old path - shouldn't happen with new monitor
                logger.warning(f"⚠️ Received helius_logs source - should be helius_events")
                return

            else:
                # PumpPortal path (fallback)
                initial_buy = token_data.get('data', {}).get('solAmount', 0) if 'data' in token_data else token_data.get('solAmount', 0)
                name = token_data.get('data', {}).get('name', '') if 'data' in token_data else token_data.get('name', '')

                if initial_buy < 0.1 or initial_buy > 10:
                    return

                if len(name) < 3:
                    return

                # Get real blockchain state (no adjustments!)
                token_age = token_data.get('age', 0) or token_data.get('token_age', 0) or 2.0

                # For very young tokens, use WebSocket directly (it's actually more current)
                if token_age < 1.0:
                    actual_sol = float(token_data.get('data', {}).get('vSolInBondingCurve', 0))
                    source_type = 'websocket_direct'
                else:
                    # Try blockchain for older tokens
                    curve_state = self.curve_reader.get_curve_state(mint, use_cache=False)

                    if curve_state and curve_state.get('is_valid'):
                        actual_sol = curve_state['sol_raised']
                        source_type = 'blockchain'
                    else:
                        actual_sol = float(token_data.get('data', {}).get('vSolInBondingCurve', 0))
                        source_type = 'websocket_fallback'

            # Slippage protection via curve reader (optional logging only)
            estimated_slippage = self.curve_reader.estimate_slippage(mint, BUY_AMOUNT_SOL)
            if estimated_slippage:
                logger.info(f"📊 Curve-based slippage estimate: {estimated_slippage:.2f}%")

            # Get token data
            token_data_ws = token_data.get('data', token_data) if 'data' in token_data else token_data
            ws_tokens = float(token_data_ws.get('vTokensInBondingCurve', 800_000_000))
            token_decimals = 6  # PumpFun ALWAYS uses 6 decimals

            # Calculate price data from current SOL
            actual_tokens_atomic = int(ws_tokens * (10 ** token_decimals))
            actual_sol_lamports = int(actual_sol * 1e9)
            price_lamports_per_atomic = (actual_sol_lamports / actual_tokens_atomic) if actual_tokens_atomic > 0 else 0

            # ✅ CORRECT: Calculate market cap from token price
            v_sol_human = actual_sol
            v_tokens_human = ws_tokens
            sol_price_usd = await self._get_sol_price_async()

            if v_tokens_human > 0:
                price_per_token_sol = v_sol_human / v_tokens_human
                total_supply = 1_000_000_000
                market_cap = total_supply * price_per_token_sol * sol_price_usd
            else:
                market_cap = 0

            curve_data = {
                'sol_raised': actual_sol,
                'sol_in_curve': actual_sol,
                'virtual_sol_reserves': actual_sol_lamports,
                'virtual_token_reserves': actual_tokens_atomic,
                'price_lamports_per_atomic': price_lamports_per_atomic,
                'source': source_type,
                'is_valid': True,
                'is_migrated': False
            }

            # Liquidity and SOL range validation (skip for helius_events - already validated)
            if source != 'helius_events':
                required_sol = BUY_AMOUNT_SOL * LIQUIDITY_MULTIPLIER

                if actual_sol < MIN_LIQUIDITY_SOL:
                    logger.warning(f"❌ Liquidity too low: {actual_sol:.4f} SOL < {MIN_LIQUIDITY_SOL} minimum")
                    return

                if actual_sol < required_sol:
                    logger.warning(f"❌ Insufficient liquidity: {actual_sol:.4f} SOL < {required_sol:.4f} ({LIQUIDITY_MULTIPLIER}x)")
                    return

                logger.info(f"✅ Liquidity OK: {actual_sol:.4f} SOL (>= {LIQUIDITY_MULTIPLIER}x {BUY_AMOUNT_SOL})")

                # SOL range check - only enter whale zone
                if actual_sol < MIN_BONDING_CURVE_SOL:
                    logger.warning(f"❌ Too early: {actual_sol:.2f} SOL < {MIN_BONDING_CURVE_SOL} min")
                    return

                if actual_sol > MAX_BONDING_CURVE_SOL:
                    logger.warning(f"❌ Too late: {actual_sol:.2f} SOL > {MAX_BONDING_CURVE_SOL} max")
                    return

                logger.info(f"✅ In whale zone: {actual_sol:.2f} SOL (range: {MIN_BONDING_CURVE_SOL}-{MAX_BONDING_CURVE_SOL})")

            estimated_slippage = self.curve_reader.estimate_slippage(mint, BUY_AMOUNT_SOL)

            logger.info(f"⚡ Using {source_type} data: {actual_sol:.4f} SOL, price={price_lamports_per_atomic:.10f} lamports/atom")
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
                    # ✅ FIXED: Linear model to avoid underestimating age on recycled liquidity
                    # Mayhem tokens pump at 3-5 SOL/s initially, then recycle between whales
                    # Use conservative 3.0 SOL/s to account for pump/dump/recycle patterns
                    token_age = max(sol_raised / 3.0, 2.0)

                    logger.info(
                        f"📊 Age estimate: {sol_raised:.2f} SOL ÷ 3.0 SOL/s "
                        f"= {token_age:.1f}s (linear conservative)"
                    )
                else:
                    token_age = 2.5
                    logger.warning(f"⚠️ No SOL raised data, using default age: {token_age:.1f}s")
            
            logger.info(f"📊 Using token age: {token_age:.1f}s for velocity check")
            logger.info(f"📊 SOL raised (from curve): {curve_data.get('sol_raised', 0):.4f}")
            logger.info(f"📊 Expected velocity: {curve_data.get('sol_raised', 0) / token_age:.2f} SOL/s")

            # Recalculate MC from final blockchain curve data
            sol_price_usd = await self._get_sol_price_async()
            entry_market_cap = self._calculate_mc_from_curve(curve_data, sol_price_usd)

            logger.info(f"📊 FINAL ENTRY CONDITIONS:")
            logger.info(f"   Market Cap: ${entry_market_cap:,.0f}")
            logger.info(f"   SOL in Curve: {curve_data['sol_raised']:.4f}")
            logger.info(f"   Velocity: {curve_data['sol_raised'] / token_age:.2f} SOL/s")

            # Velocity check (skip for helius_events - already validated by event monitor)
            if source != 'helius_events':
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

            # Store pre-trade balance for accurate P&L
            self.wallet.last_balance_before_trade = self.wallet.get_sol_balance()

            signature = await self.trader.create_buy_transaction(
                mint=mint,
                sol_amount=BUY_AMOUNT_SOL,
                bonding_curve_key=bonding_curve_key,
                slippage=30,
                urgency="buy"  # 0.001 SOL priority fee
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
                        # High slippage is normal for fast-moving tokens - don't panic sell
                        logger.warning(f"⚠️ High entry slippage: {price_diff_pct:.1f}% - price moved between detection and execution")
                        logger.warning(f"   This is normal for fast tokens - continuing with standard exit strategy")
                        expected_tokens = (actual_sol_spent * 1e9) / estimated_entry_price if estimated_entry_price > 0 else 0
                        token_shortfall = ((expected_tokens - bought_tokens) / expected_tokens * 100) if expected_tokens > 0 else 0
                        logger.warning(f"   Expected: {expected_tokens:,.0f} tokens, Received: {bought_tokens:,.0f} ({token_shortfall:.1f}% fewer)")
                        # Let position continue with normal timer/stop-loss exit strategy
            
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

                # Timer exit disabled - using whale tiered exits instead
                # variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                # position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                position.exit_time = position.entry_time + MAX_POSITION_AGE_SECONDS  # Max hold only

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
        """Monitor position - WHALE TIERED EXITS with FLATLINE DETECTION"""
        try:
            position = self.positions.get(mint)
            if not position:
                return

            logger.info(f"📈 Starting WHALE monitoring for {mint[:8]}...")
            logger.info(f"   Entry Price: {position.entry_token_price_sol:.10f} lamports/atomic")
            logger.info(f"   Max Hold: {MAX_POSITION_AGE_SECONDS}s")
            logger.info(f"   Tiers: {TIER_1_SELL_PERCENT}% @ +{TIER_1_PROFIT_PERCENT}%, {TIER_2_SELL_PERCENT}% @ +{TIER_2_PROFIT_PERCENT}%, {TIER_3_SELL_PERCENT}% @ +{TIER_3_PROFIT_PERCENT}%")
            logger.info(f"   Your Tokens: {position.remaining_tokens:,.0f}")

            check_count = 0
            consecutive_data_failures = 0

            while mint in self.positions and position.status == 'active':
                check_count += 1
                current_time = time.time()
                age = current_time - position.entry_time
                
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    # ✅ CRITICAL FIX: Use curve_reader (same as entry verification)
                    # dex.py has broken RPC client that returns wrong values
                    curve_state = self.curve_reader.get_curve_state(mint, use_cache=False)

                    if curve_state:
                        curve_data = {
                            'sol_in_curve': curve_state.get('sol_raised', 0),
                            'price_lamports_per_atomic': curve_state.get('price_lamports_per_atomic', 0),
                            'virtual_sol_reserves': curve_state.get('virtual_sol_reserves', 0),
                            'virtual_token_reserves': curve_state.get('virtual_token_reserves', 0),
                            'is_migrated': curve_state.get('complete', False),
                            'source': 'chain',
                            'is_valid': True
                        }
                        position.has_chain_price = True
                        position.last_price_source = 'chain'
                        source = 'chain'  # curve_reader always returns chain data
                    else:
                        curve_data = None

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

                    consecutive_data_failures = 0
                    position.last_valid_price = current_token_price_sol
                    position.last_price_update = time.time()

                    # ===================================================================
                    # ✅ NEW: ENTRY SLIPPAGE GATE - Exit immediately if underwater at start
                    # ===================================================================
                    if not position.first_price_check_done:
                        position.first_price_check_done = True
                        position.last_pnl_change_time = time.time()
                        position.last_recorded_pnl = price_change
                        
                        logger.info(f"📊 First price check for {mint[:8]}...")
                        logger.info(f"   Entry: {position.entry_token_price_sol:.10f} lamports/atomic")
                        logger.info(f"   Current: {current_token_price_sol:.10f} lamports/atomic")
                        logger.info(f"   P&L: {price_change:+.1f}%")
                        
                        # ✅ ENTRY SLIPPAGE GATE: If we're -15% or worse on first check, exit immediately
                        if price_change <= -15:
                            logger.warning(f"🚨 ENTRY SLIPPAGE TOO HIGH: {price_change:.1f}% - bought at top, exiting immediately")
                            await self._close_position_full(mint, reason="entry_slippage")
                            break

                    # ===================================================================
                    # ✅ NEW: FLATLINE DETECTION - Exit if stuck negative for 30s
                    # ===================================================================
                    # Check if P&L has improved by at least 2% since last check
                    if price_change > position.last_recorded_pnl + 2:
                        position.last_recorded_pnl = price_change
                        position.last_pnl_change_time = time.time()
                    
                    flatline_duration = time.time() - position.last_pnl_change_time
                    
                    # If stuck negative for 30+ seconds with no improvement, token is dead
                    if flatline_duration > 30 and price_change < 0 and not position.is_closing:
                        logger.warning(f"💀 FLATLINE DETECTED: {mint[:8]}... stuck at {price_change:.1f}% for {flatline_duration:.0f}s")
                        logger.warning(f"   No price improvement - token is dead, exiting")
                        await self._close_position_full(mint, reason="flatline")
                        break
                    
                    self.velocity_checker.update_snapshot(
                        mint,
                        current_sol_in_curve,
                        int(current_sol_in_curve / 0.4)
                    )

                    # ===================================================================
                    # EXIT RULE 1: RUG TRAP (Emergency)
                    # ===================================================================
                    rug_threshold = -50  # Real rugs drop 80-99%, not 40%
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

                    # ===================================================================
                    # EXIT RULE 2: STOP LOSS (-10%)
                    # ===================================================================
                    if price_change <= -STOP_LOSS_PERCENTAGE and not position.is_closing:
                        if not position.has_chain_price or source != 'chain':
                            logger.warning(f"🚧 STOP LOSS signal from [{source}] ignored until first [chain] tick")
                        else:
                            logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}... (on [chain] source)")
                            logger.warning(f"   P&L: {price_change:.1f}% <= -{STOP_LOSS_PERCENTAGE}%")
                            await self._close_position_full(mint, reason="stop_loss")
                            break

                    # ===================================================================
                    # CRASH DETECTION - Dump if momentum collapsed
                    # ===================================================================
                    crash_from_peak = position.max_pnl_reached - price_change
                    if (crash_from_peak > 30 and  # Dropped 30%+ from peak
                        price_change < 10 and      # And no longer solidly profitable
                        not position.is_closing):

                        logger.warning(f"🚨 MOMENTUM CRASH: {mint[:8]}... dropped {crash_from_peak:.1f}% from peak")
                        logger.warning(f"   Peak: +{position.max_pnl_reached:.1f}% → Current: {price_change:+.1f}%")
                        await self._close_position_full(mint, reason="momentum_crash")
                        break

                    # ===================================================================
                    # EXIT RULE 3: TIER 1 TAKE PROFIT (+30%)
                    # ===================================================================
                    if (price_change >= TIER_1_PROFIT_PERCENT and
                        "tier1" not in position.partial_sells and
                        "tier1" not in position.pending_sells and
                        not position.is_closing):

                        logger.info(f"💰 TIER 1 TAKE PROFIT: {price_change:+.1f}% >= {TIER_1_PROFIT_PERCENT}%")
                        await self._execute_partial_sell(
                            mint,
                            TIER_1_SELL_PERCENT,
                            "tier1",
                            price_change
                        )

                    # ===================================================================
                    # EXIT RULE 4: TIER 2 TAKE PROFIT (+60%)
                    # ===================================================================
                    if (price_change >= TIER_2_PROFIT_PERCENT and
                        "tier2" not in position.partial_sells and
                        "tier2" not in position.pending_sells and
                        not position.is_closing):

                        logger.info(f"💰 TIER 2 TAKE PROFIT: {price_change:+.1f}% >= {TIER_2_PROFIT_PERCENT}%")
                        await self._execute_partial_sell(
                            mint,
                            TIER_2_SELL_PERCENT,
                            "tier2",
                            price_change
                        )

                    # ===================================================================
                    # EXIT RULE 5: TIER 3 TAKE PROFIT (+60%) - Fire for final 20%
                    # ===================================================================
                    # Track pending + sold to decide if tier3 should fire
                    pending_percent = len(position.pending_sells) * 40  # tier1=40%, tier2=40%
                    effective_sold = position.total_sold_percent + pending_percent

                    if (price_change >= TIER_3_PROFIT_PERCENT and
                        effective_sold >= 80 and  # ✅ Count pending sells too
                        "tier3" not in position.partial_sells and
                        "tier3" not in position.pending_sells and
                        not position.is_closing):

                        logger.info(f"💰 TIER 3 TAKE PROFIT: {price_change:+.1f}% >= {TIER_3_PROFIT_PERCENT}%")
                        logger.info(f"   Closing final {100 - position.total_sold_percent:.0f}% of position")
                        await self._close_position_full(mint, reason="tier3_profit")
                        break

                    # Progress logging
                    if check_count % 3 == 1:
                        sold_pct = position.total_sold_percent
                        logger.info(
                            f"⏱️ {mint[:8]}... | P&L: {price_change:+.1f}% | "
                            f"Peak: {position.max_pnl_reached:+.1f}% | "
                            f"Sold: {sold_pct:.0f}% | Age: {age:.0f}s"
                        )
                
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

            # Check and mark pending IMMEDIATELY
            if target_name in position.pending_sells:
                logger.debug(f"{target_name} already pending for {mint[:8]}, skipping duplicate")
                return False
            position.pending_sells.add(target_name)

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
                urgency="sell"  # 0.0015 SOL priority fee for normal sells
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
                    logger.info(f"Retrying {target_name} (attempt {retry_count + 2}/3)")

                    # ✅ DON'T remove from pending_sells - keep it blocked to prevent monitor duplicates
                    # Only update pending_token_amounts for accurate tracking
                    if target_name in position.pending_token_amounts:
                        del position.pending_token_amounts[target_name]

                    token_decimals = self.wallet.get_token_decimals(mint)
                    ui_tokens_to_sell = round(position.remaining_tokens * (sell_percent / 100), token_decimals)

                    retry_signature = await self.trader.create_sell_transaction(
                        mint=mint,
                        token_amount=ui_tokens_to_sell,
                        slippage=50,
                        token_decimals=token_decimals,
                        urgency="sell"  # 0.0015 SOL priority fee for normal sell retry
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
                    
                    # ✅ Check if sell actually landed by checking token balance
                    try:
                        actual_balance = self.wallet.get_token_balance(mint)
                        expected_after_sell = position.initial_tokens * (1 - (position.total_sold_percent + sell_percent) / 100)
                        
                        if actual_balance < expected_after_sell * 0.95:  # Balance is lower than expected = sell landed
                            sold_amount = position.remaining_tokens - actual_balance
                            if sold_amount > 0:
                                logger.info(f"✅ {target_name} actually landed! Balance shows {sold_amount:,.0f} tokens sold")
                                position.partial_sells[target_name] = {
                                    'pnl': current_pnl,
                                    'time': time.time(),
                                    'percent_sold': sell_percent,
                                    'status': 'landed_late'
                                }
                                position.total_sold_percent += sell_percent
                                position.remaining_tokens = actual_balance
                    except Exception as e:
                        logger.debug(f"Balance check failed: {e}")
                    
                    if target_name in position.pending_sells:
                        position.pending_sells.remove(target_name)
                    if target_name in position.pending_token_amounts:
                        del position.pending_token_amounts[target_name]
                    
                    if target_name not in position.partial_sells:
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

            # ✅ Clear pending sells to stop background retry tasks from continuing
            position.pending_sells.clear()
            position.pending_token_amounts.clear()
            logger.debug(f"Cleared pending sells for {mint[:8]} due to {reason}")

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

            # Use emergency priority only for stop loss and rug trap
            urgency = "emergency" if reason in ["stop_loss", "rug_trap"] else "sell"

            signature = await self.trader.create_sell_transaction(
                mint=mint,
                token_amount=ui_token_balance,
                slippage=100 if is_migrated else 50,
                token_decimals=token_decimals,
                urgency=urgency  # 0.002 SOL for emergency, 0.0015 SOL for normal
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
                # Transaction parsing failed - fallback using wallet balance delta
                logger.warning("⚠️ Transaction parsing failed after 30s")
                logger.warning("   Falling back to wallet balance delta")
                logger.warning(f"   Transaction: https://solscan.io/tx/{signature}")

                # Wait for balance to update
                await asyncio.sleep(2)

                # Get current balance and calculate delta
                post_balance = self.wallet.get_sol_balance()
                pre_balance = self.wallet.last_balance_before_trade or post_balance

                # Calculate actual SOL received from wallet movement
                balance_delta = post_balance - pre_balance
                actual_sol_received = balance_delta + position.amount_sol  # Add back invested amount

                # Sanity check: if delta looks wrong, use conservative estimate
                if actual_sol_received <= 0 or actual_sol_received > position.amount_sol * 2:
                    logger.warning(f"⚠️ Suspicious balance delta: {balance_delta:.6f} SOL")
                    logger.warning(f"   Pre-trade: {pre_balance:.6f}, Post-trade: {post_balance:.6f}")
                    # Assume break-even minus small loss
                    actual_sol_received = position.amount_sol * 0.99

                actual_tokens_sold = ui_token_balance
                estimated_fees = 0.009
                trading_pnl_sol = actual_sol_received - position.amount_sol
                actual_fees_paid = estimated_fees
                gross_sale_proceeds = actual_sol_received
                final_pnl_sol = trading_pnl_sol

                logger.info(f"📊 Wallet Balance Fallback:")
                logger.info(f"   Pre-trade: {pre_balance:.6f} SOL")
                logger.info(f"   Post-trade: {post_balance:.6f} SOL")
                logger.info(f"   Delta: {balance_delta:+.6f} SOL")
                logger.info(f"   Invested: {position.amount_sol:.6f} SOL")
                logger.info(f"   Received: {actual_sol_received:.6f} SOL")
                logger.info(f"   P&L: {trading_pnl_sol:+.6f} SOL")

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
            
            from solana.rpc.api import Client
            rpc_client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
            self.scanner = HeliusLogsMonitor(self.on_token_found, rpc_client)
            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Bot running with WHALE TIERED EXITS STRATEGY")
            logger.info(f"⚡ Velocity: ≥{VELOCITY_MIN_SOL_PER_SECOND} SOL/s, ≥{VELOCITY_MIN_BUYERS} buyers")
            logger.info(f"⚡ Recent: ≥{VELOCITY_MIN_RECENT_1S_SOL} SOL (1s), ≥{VELOCITY_MIN_RECENT_3S_SOL} SOL (3s)")
            logger.info(f"💰 Tiers: 40%@+30%, 40%@+60%, 20%@+100%")
            logger.info(f"🛑 Stop loss: -{STOP_LOSS_PERCENTAGE}%")
            logger.info(f"⏱️ Max hold: {MAX_POSITION_AGE_SECONDS}s")
            logger.info(f"🎯 Circuit breaker: 3 consecutive losses")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            logger.info(
                                f"  • {mint[:8]}... | P&L: {pos.pnl_percent:+.1f}% | "
                                f"Max: {pos.max_pnl_reached:+.1f}% | "
                                f"Age: {time.time() - pos.entry_time:.0f}s | "
                                f"Sold: {pos.total_sold_percent:.0f}%"
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
