
"""
Main Orchestrator - FINAL FIX: Entry price bookkeeping + Stop loss with source checking
"""

import asyncio
import logging
import signal
import time
import random
import os
from datetime import datetime, timedelta
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
    TIER_1_SELL_PERCENT,
    TIER_2_PROFIT_PERCENT, TIER_2_SELL_PERCENT,
    # Timer exit parameters
    TIMER_EXIT_BASE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS,
    TIMER_MAX_EXTENSIONS,
    FAIL_FAST_CHECK_TIME, FAIL_FAST_PNL_THRESHOLD,
    MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL,
    SELL_BURST_EXIT_MAX_CURVE, SELL_BURST_EXIT_MIN_SELLS, SELL_BURST_EXIT_MIN_SOL,
    PROFIT_DECAY_MID_PERCENT, MID_TIER_MAX_CURVE, PROFIT_DECAY_RUNNER_PERCENT,
)

from wallet import WalletManager
from dex import PumpFunDEX
from helius_logs_monitor import HeliusLogsMonitor
from pumpportal_trader import PumpPortalTrader
from local_swap import LocalSwapBuilder
from performance_tracker import PerformanceTracker
from trade_logger import TradeLogger
from curve_reader import BondingCurveReader

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
        self.detection_curve_sol = 0  # Original detection curve (for rug detection only)

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
        logger.info("🚀 INITIALIZING SNIPER BOT")
        logger.info("=" * 60)
        
        self.wallet = WalletManager()
        self.dex = PumpFunDEX(self.wallet)
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        self.tracker = PerformanceTracker()
        self.trade_logger = TradeLogger("/data/trades_clean.csv")

        from solana.rpc.api import Client
        from config import RPC_ENDPOINT, PUMPFUN_PROGRAM_ID
        
        rpc_client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.curve_reader = BondingCurveReader(rpc_client, PUMPFUN_PROGRAM_ID)
        
        
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.trader = PumpPortalTrader(self.wallet, client)
        self.local_builder = LocalSwapBuilder(self.wallet, client)

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
        logger.info(f"  • Strategy: ⚡ HELIUS DETECTION + ORDER FLOW EXITS")
        logger.info(f"  • Detection: <100ms via logsSubscribe")
        logger.info(f"  • Entry range: {MIN_BONDING_CURVE_SOL}-{MAX_BONDING_CURVE_SOL} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Exits: Rug (40% drain), Whale (15%), Burst (6+ sells)")
        logger.info(f"  • Max hold: {MAX_POSITION_AGE_SECONDS}s")
        logger.info(f"  • Velocity gate: 2.0-15.0 SOL/s avg, ≥{VELOCITY_MIN_BUYERS} buyers")
        logger.info(f"  • Liquidity gate: {LIQUIDITY_MULTIPLIER}x buy size (min {MIN_LIQUIDITY_SOL} SOL)")
        logger.info(f"  • Max slippage: {MAX_SLIPPAGE_PERCENT}%")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Circuit breaker: 10 consecutive losses")
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

    def _check_orderflow_exit(self, mint: str, position: Position, pnl_percent: float) -> tuple:
        """
        ORDER FLOW EXIT - Momentum-based exits (restored from Dec 10-12)

        1. EMERGENCY VOLUME: 4+ SOL dumped in 5s (bypasses 8s age gate) - NO OVERRIDE
        2. PROFIT DECAY: P&L dropped to 65% of peak while still >10%
        3. NET NEGATIVE FLOW: Sells outweighing buys (momentum reversed)
        4. BUY DROUGHT: No buys for 5s while in profit (momentum dead)
        5. WHALE: Single sell > 16% of curve + declining curve (smart money exiting)

        Returns (should_exit: bool, reason: str)
        """
        if not self.scanner:
            return False, ""

        state = self.scanner.watched_tokens.get(mint, {})
        if not state:
            return False, ""

        now = time.time()
        age = now - position.entry_time

        # Get flow data
        flow_sells = state.get('flow_sells', [])
        flow_buys = state.get('flow_buys', [])

        recent_sell_volume = sum(amt for t, amt in flow_sells if now - t < 5)
        recent_buy_volume = sum(amt for t, amt in flow_buys if now - t < 5)

        # Minimum age before NORMAL exits (give position time to establish)
        min_age = 8
        if age < min_age:
            return False, ""

        # =========================================================
        # EXIT 2: PROFIT DECAY - Preserve gains when momentum dies
        # Exit when P&L drops to 65% of peak (while still >10%)
        # This catches the slow bleed after peak
        # =========================================================
        if position.max_pnl_reached >= 15:  # Only if we hit a meaningful peak
            decay_threshold = position.max_pnl_reached * 0.65
            if pnl_percent < decay_threshold and pnl_percent > 10:
                logger.warning(f"📉 PROFIT DECAY: {pnl_percent:+.1f}% < 65% of peak {position.max_pnl_reached:+.1f}%")
                logger.warning(f"   Preserving gains before further decline")
                return True, f"profit_decay_{pnl_percent:.0f}_from_{position.max_pnl_reached:.0f}"

        # =========================================================
        # EXIT 3: NET NEGATIVE FLOW - Momentum reversed
        # When sells > buys significantly, price is going DOWN
        # =========================================================
        net_flow = recent_buy_volume - recent_sell_volume

        if net_flow < -1.0 and pnl_percent > 10:  # -1 SOL negative flow while in profit
            logger.warning(f"📉 NEGATIVE FLOW: {net_flow:.2f} SOL (buys={recent_buy_volume:.2f}, sells={recent_sell_volume:.2f})")
            logger.warning(f"   Momentum reversed - exiting at {pnl_percent:+.1f}%")
            return True, f"negative_flow_{net_flow:.1f}"

        # =========================================================
        # EXIT 4: BUY DROUGHT - No new buyers, momentum dead
        # If no buys for 5s during what should be active trading = dead
        # =========================================================
        last_buy_time = state.get('last_buy_time', 0)
        time_since_buy = now - last_buy_time if last_buy_time > 0 else 0

        if time_since_buy > 5.0 and pnl_percent > 15:
            logger.warning(f"🏜️ BUY DROUGHT: No buys for {time_since_buy:.1f}s")
            logger.warning(f"   Momentum dead - exiting at {pnl_percent:+.1f}%")
            return True, f"buy_drought_{time_since_buy:.1f}s"

        # =========================================================
        # EXIT 5: WHALE EXIT - Smart money leaving
        # Single sell > 16% of curve = whale exiting
        # Only exit if we're in profit (don't panic sell at loss)
        # =========================================================
        if pnl_percent > 5:
            current_curve = state.get('vSolInBondingCurve', 0)
            recent_sells = [(t, amt) for t, amt in flow_sells if now - t < 3]

            if recent_sells and current_curve > 0:
                largest_recent = max(amt for t, amt in recent_sells)
                whale_pct = (largest_recent / current_curve) * 100

                if whale_pct >= 16:
                    # Only exit if curve is DECLINING after whale sell
                    entry_curve = getattr(position, 'entry_sol_in_curve', 0) or getattr(position, 'detection_curve_sol', 0) or 6.0
                    curve_growth = current_curve - entry_curve

                    if curve_growth <= 0:  # Curve at or below entry = real dump
                        logger.warning(f"🐋 WHALE EXIT: {largest_recent:.2f} SOL sell = {whale_pct:.0f}% of curve")
                        logger.warning(f"   Curve declining ({curve_growth:+.1f} SOL) - following smart money out")
                        return True, f"whale_exit_{whale_pct:.0f}pct"
                    else:
                        logger.info(f"⚡ Whale sell ({whale_pct:.0f}%) but curve +{curve_growth:.1f} SOL above entry - holding")

        return False, ""

    def _check_curve_exits(self, mint: str, position: Position) -> tuple:
        """
        CURVE-BASED EXIT SYSTEM - All decisions from Helius WebSocket
        NO RPC calls. Uses absolute SOL thresholds, not percentages.

        Returns: (should_exit: bool, reason: str, pnl_percent: float)
        """
        from config import (
            RUG_FLOOR_SOL, MOMENTUM_DEATH_SOL, PROFIT_PEAK_THRESHOLD_SOL,
            PROFIT_DECAY_PERCENT, WHALE_SELL_PERCENT, BUY_DROUGHT_SECONDS,
            MIN_EXIT_AGE_SECONDS
        )

        if not self.scanner:
            return False, "", 0.0

        state = self.scanner.watched_tokens.get(mint, {})
        if not state:
            return False, "", 0.0

        now = time.time()
        age = now - position.entry_time

        # Get curve values from Helius (real-time, no RPC)
        current_curve = state.get('vSolInBondingCurve', 0)

        # Use slippage-adjusted entry curve for accurate P&L (falls back to detection if not set)
        entry_curve = getattr(position, 'entry_sol_in_curve', 0) or getattr(position, 'detection_curve_sol', 0) or 6.0

        # Track peak curve
        peak_curve = state.get('peak_curve_sol', current_curve)
        if current_curve > peak_curve:
            state['peak_curve_sol'] = current_curve
            peak_curve = current_curve

        # Calculate P&L from ACTUAL fill price (not curve estimate)
        VIRTUAL_RESERVES = 30.0
        virtual_sol_lamports = (current_curve + VIRTUAL_RESERVES) * 1e9
        INITIAL_K = 30e9 * 1073000191e6  # PumpFun launch reserves product
        virtual_tokens_atomic = INITIAL_K / virtual_sol_lamports
        current_price = virtual_sol_lamports / virtual_tokens_atomic  # lamports per atomic

        entry_price = getattr(position, 'entry_token_price_sol', 0)
        if entry_price > 0:
            pnl_percent = ((current_price / entry_price) - 1) * 100
        else:
            pnl_percent = 0.0

        # Curve deltas
        curve_from_entry = current_curve - entry_curve
        curve_from_peak = current_curve - peak_curve

        # ===========================================
        # EXIT 1: RUG FLOOR (highest priority - always check)
        # ===========================================
        if current_curve < RUG_FLOOR_SOL:
            # SANITY CHECK: Don't rug floor if active buying
            # Batch processing can show low curve before buys in same batch update it
            flow_buys = state.get('flow_buys', [])
            recent_buy_volume = sum(amt for t, amt in flow_buys if now - t < 3)

            if recent_buy_volume >= 2.0:
                logger.info(f"⚡ Rug floor ({current_curve:.2f}) BUT {recent_buy_volume:.1f} SOL bought in 3s - HOLDING")
            else:
                logger.warning(f"🚨 RUG FLOOR: {current_curve:.2f} SOL < {RUG_FLOOR_SOL} floor")
                return True, f"rug_floor_{current_curve:.1f}", pnl_percent

        # Minimum age gate for non-emergency exits
        if age < MIN_EXIT_AGE_SECONDS:
            return False, "", pnl_percent

        # ===========================================
        # EXIT 2: MOMENTUM DEATH (lost 1.5+ SOL from entry + still falling)
        # ===========================================
        if curve_from_entry < -MOMENTUM_DEATH_SOL:
            curve_history = state.get('curve_history', [])
            if len(curve_history) >= 2:
                recent = [(t, v) for t, v in curve_history if now - t < 3]
                if len(recent) >= 2 and recent[-1][1] <= recent[0][1]:
                    logger.warning(f"📉 MOMENTUM DEATH: {curve_from_entry:+.2f} SOL from entry")
                    return True, f"momentum_death_{curve_from_entry:.1f}", pnl_percent

        # ===========================================
        # EXIT 3: WHALE DUMP (large sell + curve at/below entry)
        # ===========================================
        flow_sells = state.get('flow_sells', [])
        recent_sells = [(t, amt) for t, amt in flow_sells if now - t < 3]

        if recent_sells and current_curve > 0:
            largest_sell = max(amt for t, amt in recent_sells)
            whale_pct = (largest_sell / current_curve) * 100

            if whale_pct >= WHALE_SELL_PERCENT and curve_from_entry <= 0:
                logger.warning(f"🐋 WHALE DUMP: {largest_sell:.2f} SOL ({whale_pct:.0f}%) + curve flat")
                return True, f"whale_dump_{whale_pct:.0f}pct", pnl_percent

        # ===========================================
        # EXIT 4: SELL BURST (curve < 12 SOL) - ONLY if BELOW ENTRY
        # Profit-taking looks identical to dumps (6+ sells), but dumps go below entry
        # ===========================================
        if current_curve < SELL_BURST_EXIT_MAX_CURVE and peak_curve < SELL_BURST_EXIT_MAX_CURVE:
            flow_sells = state.get('flow_sells', [])
            # Count real sells (>0.01 SOL) in last 5 seconds
            real_sells_5s = [
                (t, amt) for t, amt in flow_sells
                if now - t < 5.0 and amt >= SELL_BURST_EXIT_MIN_SOL
            ]

            if len(real_sells_5s) >= SELL_BURST_EXIT_MIN_SELLS:
                # CRITICAL: Only exit if curve drops BELOW entry
                # Above entry = profit-taking (healthy), below entry = real dump
                if current_curve < entry_curve:
                    logger.warning(f"🔥 SELL BURST + BELOW ENTRY: {len(real_sells_5s)} sells, curve {current_curve:.1f} < entry {entry_curve:.1f}")
                    return True, f"sell_burst_underwater_{len(real_sells_5s)}_at_{current_curve:.1f}", pnl_percent
                else:
                    gain_above_entry = ((current_curve / entry_curve) - 1) * 100 if entry_curve > 0 else 0
                    logger.info(f"⚡ Sell burst ({len(real_sells_5s)}) but +{gain_above_entry:.0f}% above entry - profit-taking, holding")

        # ===========================================
        # EXIT 5: TIERED PROFIT DECAY (with Tier 2 sell burst)
        # ===========================================
        drop_percent = (peak_curve - current_curve) / peak_curve if peak_curve > 0 else 0

        # Tier 2: 12-25 SOL curve
        if peak_curve >= SELL_BURST_EXIT_MAX_CURVE and peak_curve < MID_TIER_MAX_CURVE:
            # SELL BURST + DECLINING CURVE (catch dumps before 40% decay)
            # 8+ sells in 5s AND 15%+ drop from peak AND curve still falling
            flow_sells = state.get('flow_sells', [])
            real_sells_5s = [
                (t, amt) for t, amt in flow_sells
                if now - t < 5.0 and amt >= SELL_BURST_EXIT_MIN_SOL
            ]

            if len(real_sells_5s) >= 8 and drop_percent >= 0.15:
                # Confirm curve is DECLINING (not just dipped and recovered)
                curve_history = state.get('curve_history', [])
                curve_declining = False
                if len(curve_history) >= 2:
                    recent = [(t, v) for t, v in curve_history if now - t < 5]
                    if len(recent) >= 2 and recent[-1][1] < recent[0][1]:
                        curve_declining = True

                if curve_declining:
                    logger.warning(f"🔥 TIER2 SELL BURST: {len(real_sells_5s)} sells + {drop_percent:.0%} drop + declining")
                    return True, f"tier2_sell_burst_{len(real_sells_5s)}_at_{current_curve:.1f}", pnl_percent

            # Standard decay check (40% from peak)
            if drop_percent >= PROFIT_DECAY_MID_PERCENT:
                logger.warning(f"📉 MID-TIER DECAY: Peak {peak_curve:.1f} → {current_curve:.1f} SOL ({drop_percent:.0%} drop)")
                return True, f"profit_decay_{drop_percent:.0%}_from_{peak_curve:.1f}", pnl_percent

        # Tier 3: 25+ SOL curve - 30% decay (runner, catch the top)
        elif peak_curve >= MID_TIER_MAX_CURVE:
            if drop_percent >= PROFIT_DECAY_RUNNER_PERCENT:
                logger.warning(f"📉 RUNNER DECAY: Peak {peak_curve:.1f} → {current_curve:.1f} SOL ({drop_percent:.0%} drop)")
                return True, f"profit_decay_{drop_percent:.0%}_from_{peak_curve:.1f}", pnl_percent

        # ===========================================
        # EXIT 5: BUY DROUGHT (no buys + declining)
        # ===========================================
        last_buy_time = state.get('last_buy_time', 0)
        time_since_buy = now - last_buy_time if last_buy_time > 0 else 0

        if time_since_buy > BUY_DROUGHT_SECONDS:
            curve_history = state.get('curve_history', [])
            if len(curve_history) >= 3:
                old_curves = [v for t, v in curve_history if now - t > 3]
                new_curves = [v for t, v in curve_history if now - t <= 3]

                if old_curves and new_curves:
                    old_avg = sum(old_curves) / len(old_curves)
                    new_avg = sum(new_curves) / len(new_curves)

                    if new_avg < old_avg - 0.5:
                        logger.warning(f"🏜️ BUY DROUGHT: {time_since_buy:.1f}s + curve declining")
                        return True, f"buy_drought_{time_since_buy:.0f}s", pnl_percent

        return False, "", pnl_percent

    async def _on_position_sell(self, mint: str, state: dict):
        """
        INSTANT EXIT CHECK - Called by Helius on EVERY sell event.
        Runs full exit condition check immediately (no 2-second delay).
        """
        position = self.positions.get(mint)
        if not position or position.status != 'active' or position.is_closing:
            return

        # Run FULL exit condition check instantly (same logic as monitoring loop)
        should_exit, exit_reason, pnl_percent = self._check_curve_exits(mint, position)

        if should_exit:
            logger.warning(f"⚡ INSTANT EXIT: {exit_reason} (triggered by sell event)")
            await self._close_position_full(mint, reason=exit_reason)
            return

        # Update position P&L for display
        position.pnl_percent = pnl_percent
        if pnl_percent > position.max_pnl_reached:
            position.max_pnl_reached = pnl_percent
            if not hasattr(position, 'peak_time'):
                position.peak_time = time.time()

    async def _on_position_buy(self, mint: str, state: dict):
        """
        INSTANT CHECK on buy events - detects migration threshold.
        """
        position = self.positions.get(mint)
        if not position or position.status != 'active' or position.is_closing:
            return

        current_curve = state.get('vSolInBondingCurve', 0)

        # Instant migration check
        if current_curve >= 85:
            logger.warning(f"⚡ INSTANT MIGRATION: Curve at {current_curve:.0f} SOL")
            await self._close_position_full(mint, reason="migration")

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

    def _get_curve_price(self, mint: str, use_cache: bool = False) -> Optional[float]:
        """
        SINGLE SOURCE OF TRUTH for token price.
        Returns price in lamports per atomic unit from curve_reader.

        This method is used for BOTH entry price AND current price to ensure
        they are always comparable and use the same calculation method.
        """
        try:
            curve_state = self.curve_reader.get_curve_state(mint, use_cache=use_cache)

            if not curve_state:
                logger.debug(f"No curve state for {mint[:8]}...")
                return None

            price = curve_state.get('price_lamports_per_atomic', 0)

            if price is None or price <= 0:
                logger.debug(f"Invalid price from curve state: {price}")
                return None

            return float(price)

        except Exception as e:
            logger.error(f"Error getting curve price for {mint[:8]}: {e}")
            return None

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

            # For v0 transactions, include loaded addresses from ALTs
            try:
                loaded = getattr(meta, 'loaded_addresses', None)
                if loaded:
                    if hasattr(loaded, 'writable'):
                        account_keys.extend([str(addr) for addr in loaded.writable])
                    if hasattr(loaded, 'readonly'):
                        account_keys.extend([str(addr) for addr in loaded.readonly])
            except Exception as e:
                logger.debug(f"Could not parse loaded_addresses: {e}")

            wallet_index = None
            if my_pubkey_str in account_keys:
                wallet_index = account_keys.index(my_pubkey_str)

            if wallet_index is not None and wallet_index < len(meta.pre_balances):
                pre_sol_lamports = meta.pre_balances[wallet_index]
                post_sol_lamports = meta.post_balances[wallet_index]
                sol_delta = (post_sol_lamports - pre_sol_lamports) / 1e9
            else:
                logger.warning(f"⚠️ Wallet not in TX accounts (v0 ALT) - SOL delta unknown")

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
        Robust transaction parsing - FIXED VERSION with comprehensive debugging
        """
        try:
            from solders.signature import Signature as SoldersSignature

            tx_sig = SoldersSignature.from_string(signature)
            start = time.time()
            my_pubkey_str = str(self.wallet.pubkey)

            logger.info(f"🔍 TX PARSING DEBUG for {signature[:16]}...")
            logger.info(f"   My wallet: {my_pubkey_str}")
            logger.info(f"   Token mint: {mint[:16]}...")

            # Poll for transaction to appear
            tx = None
            while time.time() - start < max_wait:
                try:
                    status = self.trader.client.get_signature_statuses([tx_sig])

                    if status and status.value and status.value[0]:
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

            if not tx:
                wait_time = time.time() - start
                logger.warning(f"⏱️ Timeout: TX never appeared after {wait_time:.1f}s")
                return {
                    "success": False,
                    "sol_received": 0,
                    "tokens_sold": 0,
                    "wait_time": wait_time
                }

            # Check if transaction failed
            if tx.transaction.meta is None:
                logger.error(f"❌ Transaction has no meta")
                return {"success": False, "sol_received": 0, "tokens_sold": 0, "wait_time": time.time() - start}

            if tx.transaction.meta.err is not None:
                logger.error(f"❌ Transaction failed on-chain: {tx.transaction.meta.err}")
                return {"success": False, "sol_received": 0, "tokens_sold": 0, "wait_time": time.time() - start}

            meta = tx.transaction.meta

            # =========================================================================
            # DEBUG: Log transaction structure
            # =========================================================================
            logger.info(f"📊 TX STRUCTURE:")
            logger.info(f"   pre_balances: {len(meta.pre_balances)} entries")
            logger.info(f"   post_balances: {len(meta.post_balances)} entries")
            logger.info(f"   pre_token_balances: {len(meta.pre_token_balances or [])} entries")
            logger.info(f"   post_token_balances: {len(meta.post_token_balances or [])} entries")

            # =========================================================================
            # METHOD 1: Build complete account list (static + loaded addresses)
            # =========================================================================
            account_keys = []

            # Get static keys from message
            try:
                message = tx.transaction.transaction.message
                static_keys = message.account_keys
                for key in static_keys:
                    # Handle ParsedAccount objects - extract pubkey attribute
                    if hasattr(key, 'pubkey'):
                        account_keys.append(str(key.pubkey))
                    else:
                        account_keys.append(str(key))
                logger.info(f"   Static accounts: {len(static_keys)}")
            except Exception as e:
                logger.error(f"   Failed to get static keys: {e}")

            # Get loaded addresses (for v0 transactions with ALTs)
            try:
                loaded = getattr(meta, 'loaded_addresses', None)
                if loaded:
                    writable = getattr(loaded, 'writable', []) or []
                    readonly = getattr(loaded, 'readonly', []) or []

                    for addr in writable:
                        account_keys.append(str(addr))
                    for addr in readonly:
                        account_keys.append(str(addr))

                    logger.info(f"   Loaded writable: {len(writable)}")
                    logger.info(f"   Loaded readonly: {len(readonly)}")
                else:
                    logger.info(f"   No loaded_addresses (legacy tx or empty)")
            except Exception as e:
                logger.error(f"   Failed to get loaded addresses: {e}")

            logger.info(f"   Total accounts: {len(account_keys)}")

            # =========================================================================
            # FIND WALLET IN ACCOUNTS
            # =========================================================================
            wallet_index = None

            # Method 1: Direct string match
            if my_pubkey_str in account_keys:
                wallet_index = account_keys.index(my_pubkey_str)
                logger.info(f"   ✅ Wallet found at index {wallet_index}")
            else:
                # Method 2: Try case-insensitive or partial match (shouldn't be needed but just in case)
                for i, key in enumerate(account_keys):
                    if key.lower() == my_pubkey_str.lower():
                        wallet_index = i
                        logger.info(f"   ✅ Wallet found (case-insensitive) at index {i}")
                        break

            if wallet_index is None:
                logger.warning(f"   ❌ Wallet NOT found in {len(account_keys)} accounts")
                # Log first few accounts for debugging
                for i, key in enumerate(account_keys[:5]):
                    logger.info(f"      Account[{i}]: {key}")
                if len(account_keys) > 5:
                    logger.info(f"      ... and {len(account_keys) - 5} more")

            # =========================================================================
            # EXTRACT SOL DELTA
            # =========================================================================
            sol_delta = 0.0

            if wallet_index is not None and wallet_index < len(meta.pre_balances):
                pre_sol = meta.pre_balances[wallet_index]
                post_sol = meta.post_balances[wallet_index]
                sol_delta = (post_sol - pre_sol) / 1e9
                logger.info(f"   SOL: {pre_sol/1e9:.6f} -> {post_sol/1e9:.6f} = {sol_delta:+.6f}")
            else:
                logger.warning(f"   ⚠️ Cannot get SOL delta from TX")

            # =========================================================================
            # EXTRACT TOKEN DELTA
            # =========================================================================
            token_delta = 0.0
            pre_token_amount = 0.0
            post_token_amount = 0.0

            # Log all token balances for debugging
            logger.info(f"   Token balances for mint {mint[:8]}...:")

            for balance in (meta.pre_token_balances or []):
                bal_mint = str(balance.mint) if hasattr(balance, 'mint') else str(getattr(balance, 'mint', ''))
                bal_owner = str(balance.owner) if hasattr(balance, 'owner') else str(getattr(balance, 'owner', ''))

                if mint in bal_mint:  # Partial match for safety
                    ui_amount = balance.ui_token_amount.ui_amount if hasattr(balance, 'ui_token_amount') else 0
                    logger.info(f"      PRE: owner={bal_owner[:16]}... amount={ui_amount}")

                    if bal_owner == my_pubkey_str or my_pubkey_str in bal_owner:
                        pre_token_amount = float(ui_amount) if ui_amount is not None else 0.0

            for balance in (meta.post_token_balances or []):
                bal_mint = str(balance.mint) if hasattr(balance, 'mint') else str(getattr(balance, 'mint', ''))
                bal_owner = str(balance.owner) if hasattr(balance, 'owner') else str(getattr(balance, 'owner', ''))

                if mint in bal_mint:
                    ui_amount = balance.ui_token_amount.ui_amount if hasattr(balance, 'ui_token_amount') else 0
                    logger.info(f"      POST: owner={bal_owner[:16]}... amount={ui_amount}")

                    if bal_owner == my_pubkey_str or my_pubkey_str in bal_owner:
                        post_token_amount = float(ui_amount) if ui_amount is not None else 0.0

            token_delta = post_token_amount - pre_token_amount
            tokens_sold = abs(token_delta) if token_delta < 0 else 0

            logger.info(f"   Tokens: {pre_token_amount:,.2f} -> {post_token_amount:,.2f} = {token_delta:+,.2f}")

            # =========================================================================
            # FALLBACK: Use wallet balance if TX parsing failed
            # =========================================================================
            if sol_delta == 0 and tokens_sold == 0:
                logger.warning(f"⚠️ TX parsing got nothing - trying wallet balance fallback")

                # Wait a moment for balance to update
                await asyncio.sleep(1.5)

                # Get current balance and compare to what we stored before the sell
                current_balance = self.wallet.get_sol_balance()

                # If we have a pre-trade balance stored, use it
                if hasattr(self.wallet, 'last_balance_before_trade'):
                    pre_balance = self.wallet.last_balance_before_trade
                    sol_delta = current_balance - pre_balance
                    logger.info(f"   Wallet fallback: {pre_balance:.6f} -> {current_balance:.6f} = {sol_delta:+.6f}")

                    # Check current token balance
                    current_tokens = self.wallet.get_token_balance(mint)
                    logger.info(f"   Current tokens in wallet: {current_tokens:,.2f}")

                    # If we have no tokens now but had tokens before, estimate tokens_sold
                    if current_tokens == 0:
                        # We sold everything
                        tokens_sold = pre_token_amount if pre_token_amount > 0 else 0
                else:
                    logger.error(f"   No pre-trade balance stored!")

            wait_time = time.time() - start

            # =========================================================================
            # RETURN RESULTS
            # =========================================================================
            success = sol_delta != 0 or tokens_sold > 0

            if success:
                logger.info(f"✅ TX parsing complete:")
                logger.info(f"   SOL delta: {sol_delta:+.6f}")
                logger.info(f"   Tokens sold: {tokens_sold:,.2f}")
            else:
                logger.error(f"❌ TX parsing FAILED - no data extracted")

            return {
                "success": success,
                "sol_received": sol_delta,
                "tokens_sold": tokens_sold,
                "wait_time": wait_time
            }

        except Exception as e:
            logger.error(f"❌ Exception in TX parsing: {e}")
            import traceback
            logger.error(traceback.format_exc())
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
                    f"🚀 Bot started\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    f"🛑 Stop loss: -{STOP_LOSS_PERCENTAGE}%\n"
                    f"⏱️ Max hold: {MAX_POSITION_AGE_SECONDS}s\n"
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
            self.scanner = HeliusLogsMonitor(
                self.on_token_found,
                rpc_client,
                exit_callback=self._on_position_sell,
                buy_callback=self._on_position_buy
            )

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
            
            if self.consecutive_losses >= 10:
                logger.warning(f"🛑 Circuit breaker activated - 10 consecutive losses")
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

                # Log ALL entry metrics for pattern analysis
                logger.info(f"📊 ENTRY PATTERN DATA:")
                logger.info(f"   sells_at_entry: {sell_count}")
                logger.info(f"   largest_buy_pct: {event_data.get('largest_buy_pct', 0):.1f}%")
                logger.info(f"   top2_concentration: {event_data.get('top2_concentration', 0):.1f}%")
                logger.info(f"   velocity: {velocity:.2f} SOL/s")
                logger.info(f"   buyer_velocity: {event_data.get('buyer_velocity', 0):.1f}/s")
                logger.info(f"   bundled: {event_data.get('same_slot', False)}")
                logger.info(f"   slot_clustering_pct: {event_data.get('slot_clustering_pct', 0):.0f}%")

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
            # Skip for helius_events - we already have accurate data, save RPC call
            if source != 'helius_events':
                estimated_slippage = self.curve_reader.estimate_slippage(mint, BUY_AMOUNT_SOL)
                if estimated_slippage:
                    logger.info(f"📊 Curve-based slippage estimate: {estimated_slippage:.2f}%")
            else:
                estimated_slippage = None

            # Get token data
            token_data_ws = token_data.get('data', token_data) if 'data' in token_data else token_data
            ws_tokens = float(token_data_ws.get('vTokensInBondingCurve', 800_000_000))
            token_decimals = 6  # PumpFun ALWAYS uses 6 decimals

            # Calculate price data from current SOL
            actual_tokens_atomic = int(ws_tokens * (10 ** token_decimals))
            virtual_sol = 30 + actual_sol  # Include 30 SOL virtual reserves
            virtual_sol_lamports = int(virtual_sol * 1e9)
            price_lamports_per_atomic = (virtual_sol_lamports / actual_tokens_atomic) if actual_tokens_atomic > 0 else 0

            # ✅ CORRECT: Calculate market cap from token price
            v_sol_human = actual_sol
            v_tokens_human = ws_tokens
            sol_price_usd = 235.0  # Hardcoded - only for logging MC, doesn't affect trades

            if v_tokens_human > 0:
                price_per_token_sol = v_sol_human / v_tokens_human
                total_supply = 1_000_000_000
                market_cap = total_supply * price_per_token_sol * sol_price_usd
            else:
                market_cap = 0

            curve_data = {
                'sol_raised': actual_sol,
                'sol_in_curve': actual_sol,
                'virtual_sol_reserves': virtual_sol_lamports,
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
            sol_price_usd = 235.0  # Hardcoded - only for logging MC, doesn't affect trades
            entry_market_cap = self._calculate_mc_from_curve(curve_data, sol_price_usd)

            logger.info(f"📊 FINAL ENTRY CONDITIONS:")
            logger.info(f"   Market Cap: ${entry_market_cap:,.0f}")
            logger.info(f"   SOL in Curve: {curve_data['sol_raised']:.4f}")
            logger.info(f"   Velocity: {curve_data['sol_raised'] / token_age:.2f} SOL/s")

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

            # Fixed position sizing - no confidence scaling
            buy_amount = BUY_AMOUNT_SOL  # Always 0.05 SOL
            slippage_bps = 5000  # 50% slippage
            logger.info(f"📊 Standard entry: {buy_amount} SOL, {slippage_bps/100:.0f}% slippage")

            # Store for accurate P&L tracking later
            _position_buy_amount = buy_amount

            # Build curve from Helius data for local TX (no RPC delay)
            helius_events = token_data.get('data', {})
            helius_sol = helius_events.get('vSolInBondingCurve', 0) if helius_events else 0
            creator = helius_events.get('creator') if helius_events else None

            signature = None
            if creator and helius_sol > 0:
                # Build curve directly from Helius data (no RPC delay)
                virtual_sol = 30 + helius_sol
                curve_data = {
                    'is_valid': True,
                    'virtual_sol_reserves': int(virtual_sol * 1e9),
                    'virtual_token_reserves': int(1_073_000_191 * 1e6 * (30 / virtual_sol)),
                    'sol_raised': helius_sol,
                }
                logger.info(f"⚡ Local TX with Helius curve: {helius_sol:.2f} SOL")

                # Get velocity from helius_events for dynamic slippage
                velocity = helius_events.get('velocity', 0.0) if helius_events else 0.0

                signature = await self.local_builder.create_buy_transaction(
                    mint=mint,
                    sol_amount=buy_amount,
                    curve_data=curve_data,
                    slippage_bps=slippage_bps,  # Base slippage, will be increased dynamically
                    creator=creator,
                    velocity=velocity
                )

            # Fallback to PumpPortal if local build fails
            if not signature:
                logger.warning("⚠️ Local TX failed, falling back to PumpPortal...")
                signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=buy_amount,
                    bonding_curve_key=bonding_curve_key,
                    slippage=slippage_bps,
                    urgency="buy"
                )
            
            bought_tokens = 0
            actual_sol_spent = buy_amount
            actual_entry_price = estimated_entry_price  # Will be updated if we get real data
            
            if signature:
                await asyncio.sleep(1.5)  # Reduced from 3s to 1.5s for faster confirmation

                txd = await self._get_transaction_deltas(signature, mint)
                
                # ✅ CRITICAL FIX: Always read actual wallet balance
                actual_wallet_balance = self.wallet.get_token_balance(mint)
                
                if txd["confirmed"] and txd["token_delta"] > 0:
                    bought_tokens = txd["token_delta"]
                    actual_sol_spent = abs(txd["sol_delta"])

                    logger.info(f"✅ Real fill from TX: {bought_tokens:,.0f} tokens for {actual_sol_spent:.6f} SOL")

                    if actual_wallet_balance > 0 and abs(actual_wallet_balance - bought_tokens) > (bought_tokens * 0.1):
                        logger.warning(f"⚠️ Wallet balance mismatch! TX says {bought_tokens:,.0f} but wallet has {actual_wallet_balance:,.0f}")
                        bought_tokens = actual_wallet_balance

                elif actual_wallet_balance > 0:
                    bought_tokens = actual_wallet_balance
                    actual_sol_spent = _position_buy_amount  # Use stored value, not config
                    logger.warning(f"⚠️ TX reading failed - using wallet balance: {bought_tokens:,.0f} tokens")

                else:
                    logger.warning("⚠️ No tokens in wallet - TX likely failed, moving on")
                    self.pending_buys -= 1
                    return

                _effective_entry_curve = None  # Will be set if high slippage detected
                if bought_tokens > 0 and actual_sol_spent > 0:
                    lamports_spent = actual_sol_spent * 1e9
                    token_atoms = bought_tokens * 1e6  # PumpFun uses 6 decimals
                    actual_entry_price = lamports_spent / token_atoms

                    logger.info(f"✅ Entry price from FILL DATA:")
                    logger.info(f"   SOL spent: {actual_sol_spent:.6f} ({lamports_spent:,.0f} lamports)")
                    logger.info(f"   Tokens: {bought_tokens:,.0f} ({token_atoms:,.0f} atomic)")
                    logger.info(f"   Fill price: {actual_entry_price:.10f} lamports/atomic")

                    if estimated_entry_price > 0:
                        entry_slippage = ((actual_entry_price / estimated_entry_price) - 1) * 100
                        logger.info(f"   Entry slippage vs detection: {entry_slippage:+.1f}%")

                        # HIGH SLIPPAGE WARNING ONLY - we already own the tokens, MUST monitor
                        if entry_slippage > 50:
                            logger.warning(f"⚠️ HIGH SLIPPAGE WARNING: {entry_slippage:.1f}% - monitoring anyway (tokens already bought)")

                        # SLIPPAGE-ADJUSTED BASELINE (for P&L only)
                        # Store detection curve separately for rug detection
                        _detection_curve = helius_sol if helius_sol > 0 else 6.0

                        if entry_slippage > 15:
                            slippage_multiplier = (1 + entry_slippage/100) ** 0.5
                            _effective_entry_curve = _detection_curve * slippage_multiplier
                            logger.info(f"📊 BASELINE ADJUSTED: {_detection_curve:.2f} → {_effective_entry_curve:.2f} SOL (slippage {entry_slippage:.0f}%)")
                        else:
                            _effective_entry_curve = None
                    else:
                        _effective_entry_curve = None
                else:
                    logger.error(f"❌ No fill data - using detection-time price (INACCURATE)")
                    actual_entry_price = estimated_entry_price

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
                position.entry_buyers = unique_buyers
                position.buy_signature = signature
                position.creator = creator  # Store for local sell TX
                # Capture entry metrics for trade logger
                position.entry_velocity = helius_events.get('velocity', 0) if helius_events else 0
                position.token_age_sec = token_age if 'token_age' in dir() else 0
                position.sells_at_entry = helius_events.get('sell_count', 0) if helius_events else 0
                # New entry pattern metrics for analysis
                _entry_sol = helius_events.get('vSolInBondingCurve', 1) if helius_events else 1
                position.largest_buy_pct = (helius_events.get('largest_buy', 0) / _entry_sol * 100) if helius_events and _entry_sol > 0 else 0
                position.top2_concentration = helius_events.get('top2_concentration', 0) if helius_events else 0
                position.bundled = helius_events.get('same_slot', False) if helius_events else False
                position.buyer_velocity = (helius_events.get('buy_count', 0) / max(token_age, 0.1)) if helius_events else 0
                position.buy_latency_ms = execution_time_ms
                position.entry_slippage_pct = entry_slippage if 'entry_slippage' in locals() else 0
                position.initial_tokens = bought_tokens
                position.remaining_tokens = bought_tokens
                position.last_valid_balance = bought_tokens
                position.entry_time = time.time()
                position.entry_token_price_sol = actual_entry_price  # ✅ Use ACTUAL entry price
                position.amount_sol = actual_sol_spent
                position.buy_amount = _position_buy_amount  # Store for accurate close P&L

                # Timer exit disabled - using whale tiered exits instead
                # variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                # position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                position.exit_time = position.entry_time + MAX_POSITION_AGE_SECONDS  # Max hold only

                # Store DETECTION curve (original, unadjusted) for rug detection
                _detection_curve_value = helius_sol if helius_sol > 0 else (token_data['data'].get('vSolInBondingCurve', 6) if 'data' in token_data else 6)
                position.detection_curve_sol = _detection_curve_value
                logger.info(f"📊 RUG BASELINE: {_detection_curve_value:.2f} SOL (real curve)")

                # Apply slippage-adjusted baseline for P&L calculation only
                if _effective_entry_curve is not None:
                    position.entry_sol_in_curve = _effective_entry_curve
                    position.entry_curve_sol = _effective_entry_curve
                elif 'data' in token_data:
                    position.entry_sol_in_curve = token_data['data'].get('vSolInBondingCurve', 30)
                    position.entry_curve_sol = helius_events.get('total_sol', 0) if helius_events else 0
                else:
                    position.entry_curve_sol = helius_events.get('total_sol', 0) if helius_events else 0

                self.positions[mint] = position
                self.total_trades += 1
                self.pending_buys -= 1

                # Mark token as having active position (prevents Helius cleanup)
                if self.scanner and mint in self.scanner.watched_tokens:
                    self.scanner.watched_tokens[mint]['has_active_position'] = True

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

                # ✅ FIX: Capture entry baseline with slippage adjustment
                # High slippage means we entered at a worse price than detection - adjust baseline
                detection_curve = token_data['data'].get('vSolInBondingCurve', 30) if 'data' in token_data else 30
                slippage_ratio = (actual_entry_price / estimated_entry_price) if estimated_entry_price > 0 else 1.0

                if slippage_ratio > 1.15:
                    # High slippage: adjust baseline using curve multiplier
                    curve_multiplier = slippage_ratio ** 0.5
                    effective_entry_curve = detection_curve * curve_multiplier
                    logger.info(f"📊 SLIPPAGE ADJUSTMENT: Detection={detection_curve:.2f} SOL, slippage={slippage_ratio:.2f}x")
                    logger.info(f"   Effective entry curve: {effective_entry_curve:.2f} SOL (multiplier={curve_multiplier:.3f})")
                    position.entry_sol_in_curve = effective_entry_curve
                    position.entry_curve_sol = effective_entry_curve
                else:
                    # Low slippage: use fresh Helius data if available
                    if self.scanner:
                        helius_state = self.scanner.watched_tokens.get(mint, {})
                        fresh_curve = helius_state.get('vSolInBondingCurve', 0)
                        if fresh_curve > 0:
                            position.entry_sol_in_curve = fresh_curve
                            position.entry_curve_sol = fresh_curve
                            logger.info(f"📊 Entry baseline updated: {fresh_curve:.2f} SOL (post-buy, slippage={slippage_ratio:.2f}x)")

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
            logger.info(f"   Exit: ORDER FLOW (sell burst / buyer death / velocity death)")
            logger.info(f"   Your Tokens: {position.remaining_tokens:,.0f}")

            check_count = 0
            consecutive_data_failures = 0

            while mint in self.positions and position.status == 'active':
                check_count += 1

                # ===================================================================
                # FIX 6: STALE DATA DETECTION - Exit if WebSocket stopped updating
                # ===================================================================
                if self.shutdown_requested or not self.running:
                    logger.warning(f"⚠️ Bot stopped while monitoring {mint[:8]} - emergency exit")
                    await self._close_position_full(mint, reason="bot_stopped")
                    break

                # Check for stale WebSocket data (no updates for 20s = data frozen)
                if self.scanner:
                    helius_state = self.scanner.watched_tokens.get(mint, {})
                    last_update = helius_state.get('last_update', 0)
                    if last_update > 0 and time.time() - last_update > 20:
                        logger.error(f"🚨 STALE DATA: No WebSocket updates for {time.time() - last_update:.0f}s")
                        logger.error(f"   Data is frozen - emergency exit to prevent holding through crash")
                        await self._close_position_full(mint, reason="stale_data")
                        break

                # Calculate P&L for display (exit checks handled by instant callbacks)
                if self.scanner:
                    helius_state = self.scanner.watched_tokens.get(mint, {})
                    current_curve = helius_state.get('vSolInBondingCurve', 0)
                    entry_curve = getattr(position, 'entry_sol_in_curve', 0) or getattr(position, 'detection_curve_sol', 0) or 6.0

                    # Calculate P&L from ACTUAL fill price (not curve estimate)
                    VIRTUAL_RESERVES = 30.0
                    if current_curve > 0:
                        virtual_sol_lamports = (current_curve + VIRTUAL_RESERVES) * 1e9
                        INITIAL_K = 30e9 * 1073000191e6  # PumpFun launch reserves product
                        virtual_tokens_atomic = INITIAL_K / virtual_sol_lamports
                        current_price = virtual_sol_lamports / virtual_tokens_atomic

                        entry_price = getattr(position, 'entry_token_price_sol', 0)
                        if entry_price > 0:
                            pnl_percent = ((current_price / entry_price) - 1) * 100
                        else:
                            pnl_percent = 0.0
                    else:
                        pnl_percent = 0.0

                    # Update position for display
                    position.pnl_percent = pnl_percent
                    if pnl_percent > position.max_pnl_reached:
                        position.max_pnl_reached = pnl_percent
                        if not hasattr(position, 'peak_time'):
                            position.peak_time = time.time()
                else:
                    pnl_percent = position.pnl_percent  # Fallback if scanner died

                # ===================================================================
                # EARLY RUG CHECK: Simple floor check (no RPC)
                # Full exit logic handled by _check_curve_exits below
                # ===================================================================
                if self.scanner and not position.is_closing:
                    state = self.scanner.watched_tokens.get(mint, {})
                    current_curve = state.get('vSolInBondingCurve', 0)

                    # Instant rug floor check - no RPC needed
                    from config import RUG_FLOOR_SOL
                    if current_curve > 0 and current_curve < RUG_FLOOR_SOL:
                        # SANITY CHECK: Don't rug floor if active buying
                        # Batch processing can show low curve before buys in same batch update it
                        flow_buys = state.get('flow_buys', [])
                        now_ts = time.time()
                        recent_buy_volume = sum(amt for t, amt in flow_buys if now_ts - t < 3)

                        if recent_buy_volume >= 2.0:
                            logger.info(f"⚡ Early rug ({current_curve:.2f}) BUT {recent_buy_volume:.1f} SOL bought in 3s - HOLDING")
                        else:
                            logger.warning(f"🚨 EARLY RUG: Curve {current_curve:.2f} < {RUG_FLOOR_SOL} floor")
                            await self._close_position_full(mint, reason="early_rug_floor")
                            break

                # Early exit if position fully sold
                if position.remaining_tokens <= 0 and not position.pending_sells:
                    logger.info(f"✅ {mint[:8]}... fully sold (remaining=0, no pending), exiting monitor")
                    position.status = 'completed'
                    break

                current_time = time.time()
                age = current_time - position.entry_time
                
                # Dynamic max age - extend for high bonding progress
                effective_max_age = MAX_POSITION_AGE_SECONDS  # Default 120s

                # Extend to 180s if high bonding progress
                if hasattr(self, 'curve_reader'):
                    try:
                        curve = self.curve_reader.get_curve_state(mint, use_cache=True)
                        if curve and curve.get('sol_raised', 0) > 0:
                            bonding_pct = (curve['sol_raised'] / 85) * 100
                            if bonding_pct >= 12:  # >12% bonding = strong momentum
                                effective_max_age = 180
                                logger.debug(f"High bonding ({bonding_pct:.1f}%): extended max age to 180s")
                    except:
                        pass

                if age > effective_max_age:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s, limit was {effective_max_age}s)")
                    position.is_closing = False  # Ensure close can execute
                    await self._close_position_full(mint, reason="max_age")
                    break

                try:
                    # Exit checks now handled by instant callbacks (_on_position_sell, _on_position_buy)
                    # This loop only handles max_age timer
                    helius_state = self.scanner.watched_tokens.get(mint, {}) if self.scanner else {}
                    current_curve_sol = helius_state.get('vSolInBondingCurve', 0)

                    # ===================================================================
                    # MAX AGE EXIT (keep this - timer based)
                    # ===================================================================
                    effective_max_age = MAX_POSITION_AGE_SECONDS
                    if current_curve_sol > 12:  # High bonding = runner
                        effective_max_age = 180

                    if age > effective_max_age:
                        logger.warning(f"⏰ MAX AGE: {age:.0f}s > {effective_max_age}s")
                        await self._close_position_full(mint, reason="max_age")
                        break

                    # ===================================================================
                    # MIGRATION CHECK (keep this)
                    # ===================================================================
                    if current_curve_sol >= 85:
                        logger.warning(f"🚀 MIGRATION: Curve at {current_curve_sol:.0f} SOL")
                        await self._close_position_full(mint, reason="migration")
                        break

                    # ===================================================================
                    # PROGRESS LOGGING
                    # ===================================================================
                    if check_count % 3 == 1:
                        entry_curve = getattr(position, 'entry_sol_in_curve', 0) or getattr(position, 'detection_curve_sol', 0) or 6.0
                        curve_delta = current_curve_sol - entry_curve

                        state = helius_state
                        sells_5s = len([t for t in state.get('sell_timestamps', []) if time.time() - t < 5])
                        buys_5s = len([t for t in state.get('buy_timestamps', []) if time.time() - t < 5])

                        logger.info(
                            f"📊 {mint[:8]}... | P&L: {pnl_percent:+.1f}% | "
                            f"Curve: {current_curve_sol:.2f} ({curve_delta:+.1f}) | "
                            f"Flow: +{buys_5s}/-{sells_5s} | Age: {age:.0f}s"
                        )

                except Exception as e:
                    logger.error(f"Error checking {mint[:8]}...: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                await asyncio.sleep(MONITOR_CHECK_INTERVAL)
            
            if mint in self.positions and position.status == 'completed':
                del self.positions[mint]
                logger.info(f"Position {mint[:8]}... removed after completion")
                
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
            token_decimals = 6  # PumpFun always 6 decimals
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
            
            # Use PumpPortal for reliable sells (no RPC failure points)
            signature = await self.trader.create_sell_transaction(
                mint=mint,
                token_amount=ui_tokens_to_sell,
                slippage=50,
                token_decimals=token_decimals,
                urgency="sell"
            )
            
            if signature and not signature.startswith("1111111"):
                # Update remaining_tokens IMMEDIATELY to prevent race condition
                position.remaining_tokens -= ui_tokens_to_sell
                logger.info(f"📊 Updated remaining_tokens: {position.remaining_tokens:,.0f} (sold {ui_tokens_to_sell:,.0f})")

                # ✅ FIX: Store pending signature for P&L recovery on early close
                if not hasattr(position, 'pending_tier_signatures'):
                    position.pending_tier_signatures = {}
                position.pending_tier_signatures[target_name] = signature

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
                    # remaining_tokens was already decremented by tokens_sold, adjust by difference
                    if actual_tokens_sold != tokens_sold:
                        adjustment = actual_tokens_sold - tokens_sold
                        position.remaining_tokens = max(0.0, position.remaining_tokens - adjustment)
                        logger.debug(f"Adjusted remaining_tokens by {adjustment:.0f} (actual vs expected)")
                
                base_sol_for_portion = position.amount_sol * (sell_percent / 100)
                actual_profit_sol = actual_sol_received - base_sol_for_portion
                
                position.sell_signatures.append(signature)
                position.realized_pnl_sol += actual_profit_sol
                self.total_realized_sol += actual_profit_sol

                # ✅ FIX: Track raw SOL received for accurate P&L on early close
                position.total_sol_received = getattr(position, 'total_sol_received', 0) + actual_sol_received
                if not hasattr(position, 'parsed_signatures'):
                    position.parsed_signatures = set()
                position.parsed_signatures.add(signature)

                position.partial_sells[target_name] = {
                    'pnl': current_pnl,
                    'time': time.time(),
                    'percent_sold': sell_percent
                }
                # Calculate actual % of original position sold (not tier's sell_percent)
                actual_sold_pct = (actual_tokens_sold / position.initial_tokens * 100) if position.initial_tokens > 0 else sell_percent
                position.total_sold_percent += actual_sold_pct
                
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
                
                if position.total_sold_percent >= 100 or position.remaining_tokens <= 0:
                    logger.info(f"✅ Position fully closed")
                    position.status = 'completed'
            else:
                logger.warning(f"❌ {target_name} RPC timeout for {mint[:8]}... checking TX status")

                # CRITICAL FIX: Check if TX EXISTS on chain before retrying
                # If signature is known to RPC (even unconfirmed), it's in mempool and will resolve
                # Only retry if TX is completely unknown

                tx_exists = False
                try:
                    from solders.signature import Signature as SoldersSignature
                    from solders.transaction_status import TransactionConfirmationStatus
                    sig_obj = SoldersSignature.from_string(signature)

                    # Check signature status - this tells us if TX is known to the network
                    status_check = self.trader.client.get_signature_statuses([sig_obj])

                    if status_check and status_check.value and status_check.value[0] is not None:
                        tx_exists = True
                        status_info = status_check.value[0]
                        logger.info(f"✅ TX exists on chain (status: {status_info.confirmation_status})")

                        # TX is in the system - it WILL resolve, don't retry
                        if status_info.err:
                            logger.error(f"❌ TX failed on-chain: {status_info.err}")
                            # TX failed definitively - clean up
                            if target_name in position.pending_sells:
                                position.pending_sells.remove(target_name)
                            if target_name in position.pending_token_amounts:
                                del position.pending_token_amounts[target_name]
                            position.remaining_tokens += tokens_sold
                            logger.warning(f"📊 Restored remaining_tokens after TX failure: {position.remaining_tokens:,.0f}")
                        else:
                            # TX pending or confirmed - wait for it, don't retry
                            logger.info(f"⏳ TX in mempool/confirmed - waiting for resolution, NOT retrying")
                            # Keep in pending_sells so monitor doesn't re-trigger
                            # Background: TX will either confirm (tokens sold) or expire (can retry next cycle)

                            # If Finalized, mark tier complete so next tier can trigger
                            if status_info.confirmation_status == TransactionConfirmationStatus.Finalized:
                                # Add to partial_sells (what tier2 actually checks)
                                position.partial_sells[target_name] = {
                                    'pnl': current_pnl,
                                    'time': time.time(),
                                    'percent_sold': sell_percent,
                                    'status': 'chain_confirmed'
                                }
                                position.total_sold_percent += sell_percent
                                # Remove from pending so next tier isn't blocked
                                if target_name in position.pending_sells:
                                    position.pending_sells.remove(target_name)

                                # TX confirmed on chain - retry parsing to capture proceeds
                                retry_result = await self._get_transaction_proceeds_robust(
                                    signature=signature,
                                    mint=mint,
                                    max_wait=15
                                )
                                if retry_result["success"] and retry_result["sol_received"] > 0:
                                    tier_proceeds = retry_result["sol_received"]
                                    position.total_sol_received = getattr(position, 'total_sol_received', 0) + tier_proceeds
                                    logger.info(f"✅ Captured tier proceeds: +{tier_proceeds:.6f} SOL")
                                else:
                                    logger.warning(f"⚠️ Could not parse tier proceeds, will be missing from P&L")

                                logger.info(f"✅ {target_name} marked complete via chain confirmation (sold {sell_percent}%)")

                                # Check if position is fully closed (but not if other tiers pending!)
                                if (position.remaining_tokens <= 0 or position.total_sold_percent >= 100) and not position.pending_sells:
                                    logger.info(f"✅ Position fully closed via chain confirmation")
                                    position.status = 'completed'
                                elif position.pending_sells:
                                    logger.info(f"⏳ Other tiers still pending: {position.pending_sells} - not closing yet")
                        return

                except Exception as e:
                    logger.warning(f"Could not check TX status: {e}")

                if not tx_exists:
                    # TX completely unknown to network - safe to retry
                    logger.info(f"🔄 TX not found in network - safe to retry")

                    retry_count = position.retry_counts.get(target_name, 0)
                    if retry_count < 2:
                        position.retry_counts[target_name] = retry_count + 1
                        logger.info(f"Retrying {target_name} (attempt {retry_count + 2}/3)")

                        token_decimals = 6  # PumpFun always 6 decimals
                        ui_tokens_to_sell = tokens_sold

                        # Use PumpPortal for reliable retry
                        retry_signature = await self.trader.create_sell_transaction(
                            mint=mint,
                            token_amount=ui_tokens_to_sell,
                            slippage=50,
                            token_decimals=token_decimals,
                            urgency="sell"
                        )

                        if retry_signature and not retry_signature.startswith("1111111"):
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
                        position.remaining_tokens += tokens_sold
                        logger.warning(f"📊 Restored remaining_tokens after max retries: {position.remaining_tokens:,.0f}")
                
        except Exception as e:
            logger.error(f"Confirmation error for {mint[:8]}: {e}")
            if mint in self.positions:
                position = self.positions[mint]
                if target_name in position.pending_sells:
                    position.pending_sells.remove(target_name)
                if target_name in position.pending_token_amounts:
                    del position.pending_token_amounts[target_name]

                # Restore tokens that were pre-decremented since sell failed
                position.remaining_tokens += tokens_sold
                logger.warning(f"📊 Restored remaining_tokens after error: {position.remaining_tokens:,.0f}")

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
            
            if ui_token_balance <= 1:
                # Tiers already sold everything - but need to parse pending TXs first
                logger.info(f"✅ Already fully exited via tiers for {mint[:8]}...")

                # Parse any pending tier TXs to capture their proceeds
                if position.sell_signatures:
                    parsed_sigs = getattr(position, 'parsed_signatures', set())
                    for sig in position.sell_signatures:
                        if sig not in parsed_sigs:
                            logger.info(f"⏳ Parsing tier TX: {sig[:16]}...")
                            result = await self._get_transaction_proceeds_robust(sig, mint, max_wait=15)
                            if result["success"] and result["sol_received"] > 0:
                                position.total_sol_received = getattr(position, 'total_sol_received', 0) + result["sol_received"]
                                logger.info(f"   ✅ Captured: +{result['sol_received']:.6f} SOL")
                                if not hasattr(position, 'parsed_signatures'):
                                    position.parsed_signatures = set()
                                position.parsed_signatures.add(sig)

                # Calculate final P&L from tier proceeds
                accumulated_tier_proceeds = getattr(position, 'total_sol_received', 0)
                hold_time = time.time() - position.entry_time
                estimated_fees = 0.006  # ~2 tier sells worth of fees
                final_pnl_sol = accumulated_tier_proceeds - position.amount_sol

                logger.info(f"📊 Final P&L from tiers:")
                logger.info(f"   Tier proceeds: {accumulated_tier_proceeds:.6f} SOL")
                logger.info(f"   Invested: {position.amount_sol:.6f} SOL")
                logger.info(f"   Trading P&L: {final_pnl_sol:+.6f} SOL")

                # Update stats
                if final_pnl_sol > 0:
                    self.profitable_trades += 1
                    self.consecutive_losses = 0
                else:
                    self.consecutive_losses += 1
                    self.session_loss_count += 1

                position.realized_pnl_sol = final_pnl_sol
                self.total_realized_sol += final_pnl_sol
                position.status = 'closed'

                # Log to tracker
                self.tracker.log_sell_executed(
                    mint=mint,
                    tokens_sold=0,
                    signature="tiers_complete",
                    sol_received=accumulated_tier_proceeds,
                    pnl_sol=final_pnl_sol,
                    fees_paid=estimated_fees,
                    pnl_percent=position.pnl_percent,
                    hold_time_seconds=hold_time,
                    reason=f"{reason}_tiers_complete",
                    max_pnl_reached=position.max_pnl_reached,
                    exit_price=position.current_price,
                    mc_at_exit=getattr(position, 'current_market_cap', 0)
                )

                # Send telegram notification
                if self.telegram:
                    emoji = "💰" if final_pnl_sol > 0 else "🔴"
                    msg = (
                        f"{emoji} POSITION CLOSED (tiers complete)\n"
                        f"Token: {mint[:16]}\n"
                        f"Reason: {reason}\n"
                        f"Hold: {hold_time:.1f}s\n"
                        f"P&L: {position.pnl_percent:+.1f}%\n"
                        f"Realized: {final_pnl_sol:+.4f} SOL"
                    )
                    await self.telegram.send_message(msg)

                # Cleanup position tracking
                if mint in self.positions:
                    del self.positions[mint]
                    logger.info(f"Active: {len(self.positions)}/{MAX_POSITIONS}")
                return
            
            hold_time = time.time() - position.entry_time
            
            logger.info(f"📤 Closing position {mint[:8]}...")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Hold time: {hold_time:.1f}s")
            logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
            logger.info(f"   Max P&L reached: {position.max_pnl_reached:+.1f}%")

            # Use remaining_tokens tracker (updated when tier sells submit)
            # This prevents double-selling when rug detected during tier confirmation
            ui_token_balance = position.remaining_tokens

            # Skip RPC for emergency exits - 416ms delay is fatal during dumps
            emergency_reasons = ["whale_dump", "rug_floor", "stop_loss", "early_rug", "momentum_death", "rug_", "stale_data"]
            is_emergency = any(r in reason for r in emergency_reasons)

            if is_emergency:
                logger.info(f"⚡ EMERGENCY EXIT: Using tracker balance {ui_token_balance:,.2f} (skipping RPC)")
            else:
                # Sanity check against wallet (only for non-emergency exits)
                actual_wallet = self.wallet.get_token_balance(mint)
                if actual_wallet > 0 and actual_wallet < ui_token_balance:
                    ui_token_balance = actual_wallet
                    logger.info(f"💰 Wallet balance lower than tracker: {actual_wallet:,.2f}")
                else:
                    logger.info(f"💰 Selling from tracker: {ui_token_balance:,.2f} tokens")

            # Capture exit decision metrics BEFORE sell TX
            helius_state = self.scanner.watched_tokens.get(mint, {}) if self.scanner else {}
            position.exit_decision_time = time.time()
            position.exit_curve_decision = helius_state.get('vSolInBondingCurve', 0)
            position.sell_start_time = time.time()

            # Use Helius real-time curve data for sell (chain RPC is 2-13s stale)
            helius_curve_sol = helius_state.get('vSolInBondingCurve', 0)

            if helius_curve_sol > 0:
                # Build curve_data from Helius (real-time, not stale RPC)
                virtual_sol = 30 + helius_curve_sol
                virtual_sol_lamports = int(virtual_sol * 1e9)
                # Constant product: k = 30 * 1_073_000_191 * 1e6
                INITIAL_K = 30 * 1_073_000_191 * 1e15
                virtual_tokens_atomic = int(INITIAL_K / virtual_sol_lamports)

                curve_data = {
                    'virtual_sol_reserves': virtual_sol_lamports,
                    'virtual_token_reserves': virtual_tokens_atomic,
                    'sol_in_curve': helius_curve_sol,
                    'is_valid': True,
                    'is_migrated': False,
                    'source': 'helius'
                }
                logger.info(f"⚡ Sell using Helius curve: {helius_curve_sol:.2f} SOL (real-time)")
            else:
                # Fallback to chain RPC only if Helius unavailable
                curve_data = self.dex.get_bonding_curve_data(mint, prefer_chain=True)
                if curve_data:
                    logger.warning(f"⚠️ Sell using chain RPC (Helius unavailable): {curve_data.get('sol_in_curve', 0):.2f} SOL")

            is_migrated = curve_data is None or curve_data.get('is_migrated', False)

            token_decimals = 6  # PumpFun always 6 decimals

            # Use emergency priority only for stop loss and rug trap
            urgency = "emergency" if reason in ["stop_loss", "rug_trap"] else "sell"

            # Capture balance RIGHT BEFORE sell for accurate P&L
            pre_close_balance = self.wallet.get_sol_balance()

            # ===== TRY LOCAL SELL FIRST (faster - same as buys) =====
            signature = None

            # Build curve_data for local builder from Helius state
            if curve_data and curve_data.get('is_valid'):
                local_curve_data = {
                    'virtual_sol_reserves': curve_data.get('virtual_sol_reserves', 0),
                    'virtual_token_reserves': curve_data.get('virtual_token_reserves', 0),
                }
            else:
                # Build from helius_curve_sol if available
                local_curve_data = None
                if helius_curve_sol > 0:
                    virtual_sol = 30 + helius_curve_sol
                    virtual_sol_lamports = int(virtual_sol * 1e9)
                    INITIAL_K = 30 * 1_073_000_191 * 1e15
                    virtual_tokens_atomic = int(INITIAL_K / virtual_sol_lamports)
                    local_curve_data = {
                        'virtual_sol_reserves': virtual_sol_lamports,
                        'virtual_token_reserves': virtual_tokens_atomic,
                    }

            # Try local sell (Jito first, same as buys)
            if local_curve_data:
                try:
                    # Use 95% slippage for all exits
                    sell_slippage = 9500

                    signature = await self.local_builder.create_sell_transaction(
                        mint=mint,
                        token_amount_ui=ui_token_balance,
                        curve_data=local_curve_data,
                        slippage_bps=sell_slippage,
                        token_decimals=6,
                        creator=position.creator
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Local sell failed: {e}")
                    signature = None

            # ===== FALLBACK TO PUMPPORTAL IF LOCAL FAILED =====
            if not signature:
                logger.warning(f"⚠️ Local sell failed, falling back to PumpPortal...")
                signature = await self.trader.create_sell_transaction(
                    mint=mint,
                    token_amount=ui_token_balance,
                    slippage=95,
                    token_decimals=token_decimals,
                    urgency=urgency
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
                return

            # ✅ NON-BLOCKING: Spawn background task for TX confirmation and P&L logging
            # This allows entry detection to resume immediately (~500ms after sell TX submission)
            logger.info(f"📤 Sell TX submitted: {signature[:16]}... (confirming in background)")
            logger.info(f"🔗 Solscan: https://solscan.io/tx/{signature}")

            asyncio.create_task(
                self._finalize_close_background(
                    mint=mint,
                    reason=reason,
                    signature=signature,
                    hold_time=hold_time,
                    ui_token_balance=ui_token_balance
                )
            )
            # Return immediately - background task handles the rest
            return

        except Exception as e:
            logger.error(f"Failed to close {mint[:8]}...: {e}")
            import traceback
            logger.error(traceback.format_exc())
            if mint in self.positions:
                self.positions[mint].status = 'error'
                del self.positions[mint]
    
    async def _finalize_close_background(
        self,
        mint: str,
        reason: str,
        signature: str,
        hold_time: float,
        ui_token_balance: float
    ):
        """
        Background task to finalize position close after TX submission.
        Handles TX confirmation, parsing, P&L calculation, and cleanup.
        Runs in background so entry detection isn't blocked.
        """
        try:
            position = self.positions.get(mint)
            if not position:
                logger.warning(f"Position {mint[:8]} already removed during background finalization")
                return

            # ✅ ROBUST: Parse transaction directly (NO wallet balance delta!)
            logger.info(f"⏳ Parsing transaction proceeds from blockchain...")
            logger.info(f"🔗 Solscan: https://solscan.io/tx/{signature}")

            tx_result = await self._get_transaction_proceeds_robust(signature, mint, max_wait=30)

            # FIX: Retry sell if TX failed (error 3005 = slippage exceeded)
            if not tx_result["success"]:
                logger.warning("⚠️ First sell failed, retrying...")
                await asyncio.sleep(0.5)
                retry_balance = self.wallet.get_token_balance(mint)

                if retry_balance > 1:
                    logger.info(f"🔄 {retry_balance:,.0f} tokens still in wallet, retry with 95% slippage")
                    retry_sig = await self.trader.create_sell_transaction(
                        mint=mint,
                        token_amount=retry_balance,
                        slippage=95,
                        token_decimals=6,
                        urgency="emergency"
                    )
                    if retry_sig:
                        logger.info(f"🔄 Retry TX: {retry_sig[:16]}...")
                        signature = retry_sig
                        tx_result = await self._get_transaction_proceeds_robust(signature, mint, max_wait=30)
                    else:
                        logger.warning(f"⚠️ Retry sell failed")

            if tx_result["success"]:
                # Got EXACT proceeds from transaction
                final_sol_received = tx_result["sol_received"]
                actual_tokens_sold = tx_result["tokens_sold"]

                logger.info(f"✅ Transaction parsing successful:")
                logger.info(f"   Wait time: {tx_result['wait_time']:.1f}s")
                logger.info(f"   SOL received: {final_sol_received:+.6f} SOL")
                logger.info(f"   Tokens sold: {actual_tokens_sold:,.2f}")

                # ✅ FIX: Parse pending tier TXs before calculating P&L
                pending_sigs = getattr(position, 'pending_tier_signatures', {})
                parsed_sigs = getattr(position, 'parsed_signatures', set())

                for tier_name, sig in list(pending_sigs.items()):
                    if sig not in parsed_sigs:
                        logger.info(f"⏳ Parsing pending {tier_name} TX: {sig[:16]}...")
                        result = await self._get_transaction_proceeds_robust(sig, mint, max_wait=15)
                        if result["success"] and result["sol_received"] > 0:
                            position.total_sol_received = getattr(position, 'total_sol_received', 0) + result["sol_received"]
                            logger.info(f"   ✅ Captured {tier_name} proceeds: +{result['sol_received']:.6f} SOL")
                            if not hasattr(position, 'parsed_signatures'):
                                position.parsed_signatures = set()
                            position.parsed_signatures.add(sig)

                # Include accumulated tier proceeds in total
                accumulated_tier_proceeds = getattr(position, 'total_sol_received', 0)
                total_sol_received = final_sol_received + accumulated_tier_proceeds

                # Calculate accurate P&L
                estimated_fees = 0.009
                trading_pnl_sol = total_sol_received - position.amount_sol

                logger.info(f"📊 P&L Calculation:")
                if accumulated_tier_proceeds > 0:
                    logger.info(f"   Tier proceeds: {accumulated_tier_proceeds:.6f} SOL")
                    logger.info(f"   Final proceeds: {final_sol_received:.6f} SOL")
                    logger.info(f"   Total received: {total_sol_received:.6f} SOL")
                else:
                    logger.info(f"   SOL received: {total_sol_received:.6f}")
                logger.info(f"   SOL invested: {position.amount_sol:.6f}")
                logger.info(f"   Trading P&L: {trading_pnl_sol:+.6f} SOL")
                logger.info(f"   Estimated fees: {estimated_fees:.6f} SOL")

                gross_sale_proceeds = total_sol_received
                actual_fees_paid = estimated_fees
                final_pnl_sol = trading_pnl_sol

            else:
                # Transaction parsing failed - DO NOT use wallet balance delta (contamination risk)
                logger.warning("⚠️ Transaction parsing failed after 30s")
                logger.warning("   NOT using wallet delta (contamination from other trades)")
                logger.warning(f"   Transaction: https://solscan.io/tx/{signature}")

                # Mark as unknown - don't fabricate P&L numbers
                actual_tokens_sold = ui_token_balance
                estimated_fees = 0.009
                actual_fees_paid = estimated_fees
                gross_sale_proceeds = 0
                final_pnl_sol = 0  # Unknown - don't add to totals

                # Flag this trade as having unknown P&L
                position.pnl_unknown = True

                logger.warning(f"📊 P&L UNKNOWN for this trade - TX parsing failed")
                logger.warning(f"   Position will be closed but P&L not counted in session totals")

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

            # Log completed trade to clean CSV with all metrics
            helius_state = self.scanner.watched_tokens.get(mint, {}) if self.scanner else {}
            exit_curve_final = helius_state.get('vSolInBondingCurve', 0)
            entry_slippage_pct = getattr(position, 'entry_slippage_pct', 0)
            sell_latency_ms = (time.time() - position.sell_start_time) * 1000 if hasattr(position, 'sell_start_time') else 0
            peak_time_sec = (position.peak_time - position.entry_time) if hasattr(position, 'peak_time') else 0
            peak_to_exit_sec = (position.exit_decision_time - position.peak_time) if hasattr(position, 'peak_time') and hasattr(position, 'exit_decision_time') else 0

            self.trade_logger.log_trade(
                mint=mint,
                entry_curve=getattr(position, 'detection_curve_sol', 0),
                peak_curve=helius_state.get('peak_curve_sol', 0),
                exit_curve_decision=getattr(position, 'exit_curve_decision', 0),
                exit_curve_final=exit_curve_final,
                entry_buyers=getattr(position, 'entry_buyers', 0),
                entry_velocity=getattr(position, 'entry_velocity', 0),
                buyer_velocity=getattr(position, 'buyer_velocity', 0),
                token_age_sec=getattr(position, 'token_age_sec', 0),
                sells_at_entry=getattr(position, 'sells_at_entry', 0),
                largest_buy_pct=getattr(position, 'largest_buy_pct', 0),
                top2_concentration=getattr(position, 'top2_concentration', 0),
                bundled=getattr(position, 'bundled', False),
                exit_reason=reason,
                hold_secs=hold_time,
                peak_time_sec=peak_time_sec,
                peak_to_exit_sec=peak_to_exit_sec,
                sells_survived=helius_state.get('sell_count', 0),
                invested=position.amount_sol,
                received=total_sol_received,
                max_pnl_pct=position.max_pnl_reached,
                entry_slippage_pct=entry_slippage_pct,
                buy_latency_ms=getattr(position, 'buy_latency_ms', 0),
                sell_latency_ms=sell_latency_ms
            )

            # Only add to totals if P&L is known
            if not getattr(position, 'pnl_unknown', False):
                self.total_realized_sol += final_pnl_sol
            else:
                logger.warning(f"⚠️ Not adding unknown P&L to session totals")

            self.tracker.log_sell_executed(
                mint=mint,
                tokens_sold=actual_tokens_sold,
                signature=signature,
                sol_received=gross_sale_proceeds,
                pnl_sol=final_pnl_sol,
                fees_paid=actual_fees_paid,
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
                    msg += f"\n⚠️ Losses: {self.consecutive_losses}/10"
                await self.telegram.send_message(msg)

            # Clear active position flag so Helius can cleanup
            if self.scanner and mint in self.scanner.watched_tokens:
                self.scanner.watched_tokens[mint]['has_active_position'] = False

            if mint in self.positions:
                del self.positions[mint]
                logger.info(f"Active: {len(self.positions)}/{MAX_POSITIONS}")

        except Exception as e:
            logger.error(f"Background close finalization failed for {mint[:8]}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Clear active position flag so Helius can cleanup
            if self.scanner and mint in self.scanner.watched_tokens:
                self.scanner.watched_tokens[mint]['has_active_position'] = False
            # Cleanup position on error
            if mint in self.positions:
                self.positions[mint].status = 'error'
                del self.positions[mint]
                logger.info(f"Removed errored position. Active: {len(self.positions)}/{MAX_POSITIONS}")

    async def _close_position(self, mint: str, reason: str = "manual"):
        """Wrapper for telegram compatibility"""
        await self._close_position_full(mint, reason)
    
    async def run(self):
        """Main run loop"""
        self.running = True
        
        try:
            await self.initialize_telegram()

            # Start blockhash cache for faster TX builds (~200-300ms savings per TX)
            await self.local_builder.start_blockhash_cache()

            from solana.rpc.api import Client
            rpc_client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
            self.scanner = HeliusLogsMonitor(
                self.on_token_found,
                rpc_client,
                exit_callback=self._on_position_sell,
                buy_callback=self._on_position_buy
            )
            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Bot running - ORDER FLOW EXITS")
            logger.info(f"🎯 Exits: Rug (40% drain), Whale (15%), Burst (6+ sells)")
            logger.info(f"🛑 Stop loss: -{STOP_LOSS_PERCENTAGE}%")
            logger.info(f"⏱️ Max hold: {MAX_POSITION_AGE_SECONDS}s")
            
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
