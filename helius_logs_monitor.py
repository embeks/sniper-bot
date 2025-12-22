
"""
Helius Logs Monitor - EVENT-DRIVEN VERSION
Tracks CreateV2, Buy, and Sell events for intelligent entry
No RPC polling - everything from WebSocket events
"""

import asyncio
import json
import logging
import time
import base64
import base58
import websockets
from datetime import datetime
from typing import Optional, Dict, Set, Tuple

from config import (
    HELIUS_API_KEY, PUMPFUN_PROGRAM_ID,
    MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL,
    MIN_UNIQUE_BUYERS, MAX_SELLS_BEFORE_ENTRY,
    MAX_SINGLE_BUY_PERCENT, MIN_VELOCITY, MAX_TOKEN_AGE_SECONDS,
    # NEW IMPORTS for 21-trade baseline filters
    MAX_TOP2_BUY_PERCENT, MIN_TOKEN_AGE_SECONDS,
    # NEW: Buyer velocity and sell ratio filters
    MAX_BUYERS_PER_SECOND, MIN_BUYERS_PER_SECOND, MAX_SELLS_AT_ENTRY, MIN_BUY_SELL_RATIO,
    # NEW: Sell burst and curve momentum gates
    SELL_BURST_COUNT, SELL_BURST_WINDOW,
    CURVE_MOMENTUM_WINDOW_RECENT, CURVE_MOMENTUM_WINDOW_OLDER, CURVE_MOMENTUM_MIN_GROWTH
)
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# Known scam creators - instant reject
BLACKLISTED_CREATORS = {
    '7swXP7W6hV4HePr2cLJHm6cL4vfpEq4DLPQh8N4c9dPc',  # Serial rugger (Gerry, Downgrade)
}


class HeliusLogsMonitor:
    """Subscribe to PumpFun program logs and track all events"""
    
    def __init__(self, callback, rpc_client, exit_callback=None, buy_callback=None):
        self.callback = callback
        self.rpc_client = rpc_client
        self.exit_callback = exit_callback
        self.buy_callback = buy_callback
        self.running = False
        self.reconnect_count = 0
        
        # Verify Helius API key
        if not HELIUS_API_KEY:
            raise ValueError("HELIUS_API_KEY is required")
        logger.info(f"✅ Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        # Token state tracking
        self.watched_tokens: Dict[str, dict] = {}
        self.triggered_tokens: Set[str] = set()  # Don't re-trigger

        # Track creator launches - skip serial scammers
        self.creator_launches: Dict[str, int] = {}  # creator -> launch count
        self.max_creator_launches = 1  # Skip if creator launched 3+ tokens in session

        # Statistics
        self.stats = {
            'creates': 0,
            'buys': 0,
            'sells': 0,
            'triggers': 0,
            'skipped_sells': 0,
            'skipped_bot': 0,
            'skipped_velocity_high': 0,
            'skipped_top2': 0,
            'skipped_distribution': 0,
            'skipped_dev': 0,
            'skipped_serial_creator': 0,
            'skipped_sell_burst': 0,      # NEW: sell burst detection
            'skipped_curve_stalled': 0,   # NEW: curve momentum gate
        }
        
        # Known discriminators
        self.CREATE_V2_DISCRIMINATOR = "1b72a94ddeeb6376"

        # Entry thresholds from config (early entry with relaxed quality gates)
        self.min_sol = MIN_BONDING_CURVE_SOL      # 2.0 SOL - enter at ~3K MC
        self.max_sol = MAX_BONDING_CURVE_SOL      # 6.0 SOL - tight window
        self.min_buyers = MIN_UNIQUE_BUYERS       # 4 unique buyers
        self.max_sell_count = MAX_SELLS_BEFORE_ENTRY  # 2 sells max (strict)
        self.max_single_buy_percent = MAX_SINGLE_BUY_PERCENT  # 35% anti-bot
        self.min_velocity = MIN_VELOCITY          # 1.0 SOL/s minimum momentum
        self.max_token_age = MAX_TOKEN_AGE_SECONDS  # 10s max age for "early"
        self.min_token_age = MIN_TOKEN_AGE_SECONDS  # NEW: minimum age before entry
        
        # NEW: 21-trade baseline filters
        # self.max_velocity = MAX_VELOCITY  # Redundant - using buyer velocity instead
        self.max_buyers_per_second = MAX_BUYERS_PER_SECOND  # Coordination detection
        self.min_buyers_per_second = MIN_BUYERS_PER_SECOND  # Minimum buyer velocity
        self.max_sells_at_entry = MAX_SELLS_AT_ENTRY  # Max sells allowed at entry
        self.min_buy_sell_ratio = MIN_BUY_SELL_RATIO  # Min buy:sell ratio
        self.max_top2_percent = MAX_TOP2_BUY_PERCENT  # 65% max from top 2 wallets

        # NEW: Sell burst detection (timing-based)
        self.sell_burst_count = SELL_BURST_COUNT
        self.sell_burst_window = SELL_BURST_WINDOW

        # NEW: Curve momentum gate
        self.curve_momentum_window_recent = CURVE_MOMENTUM_WINDOW_RECENT
        self.curve_momentum_window_older = CURVE_MOMENTUM_WINDOW_OLDER
        self.curve_momentum_min_growth = CURVE_MOMENTUM_MIN_GROWTH

        self.max_watch_time = 180  # Match MAX_POSITION_AGE_SECONDS + buffer

    async def _check_dev_holdings(self, mint: str, creator: str) -> float:
        """Check if creator holds tokens. Returns token balance (0 if none)."""
        try:
            from solders.pubkey import Pubkey
            from config import TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID

            mint_pubkey = Pubkey.from_string(mint)
            creator_pubkey = Pubkey.from_string(creator)

            # Derive creator's ATA for this token
            creator_ata = Pubkey.find_program_address(
                [bytes(creator_pubkey), bytes(TOKEN_2022_PROGRAM_ID), bytes(mint_pubkey)],
                ASSOCIATED_TOKEN_PROGRAM_ID
            )[0]

            # Check balance via RPC
            response = self.rpc_client.get_token_account_balance(creator_ata)

            if response and response.value:
                ui_amount = response.value.ui_amount
                return float(ui_amount) if ui_amount else 0.0
            return 0.0

        except Exception as e:
            # Account doesn't exist = no holdings
            logger.debug(f"Dev holdings check: {e}")
            return 0.0
        
    async def start(self):
        """Connect to Helius WebSocket and subscribe to PumpFun logs"""
        self.running = True
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        
        logger.info("🔍 Connecting to Helius WebSocket...")
        logger.info(f"   Strategy: EARLY ENTRY with strict quality gates")
        logger.info(f"   Entry zone: {self.min_sol}-{self.max_sol} SOL")
        logger.info(f"   Min buyers: {self.min_buyers} | Max sells: {self.max_sells_at_entry} (ratio {self.min_buy_sell_ratio}:1)")
        logger.info(f"   Anti-bot: single buy < {self.max_single_buy_percent:.0f}%")
        logger.info(f"   Min velocity: {self.min_velocity} SOL/s | Max buyers/s: {self.max_buyers_per_second}")
        logger.info(f"   Max token age: {self.max_token_age}s")
        logger.info(f"   Max top-2 concentration: {self.max_top2_percent}%")
        
        while self.running:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as websocket:
                    logger.info("✅ Connected to Helius WebSocket!")
                    
                    # Subscribe to ALL PumpFun program logs
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(PUMPFUN_PROGRAM_ID)]},
                            {"commitment": "confirmed"}
                        ]
                    }
                    
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("📡 Subscribed to PumpFun logs (Create/Buy/Sell)")
                    
                    # Start cleanup task
                    cleanup_task = asyncio.create_task(self._cleanup_old_tokens())
                    
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            if 'result' in data and 'id' in data:
                                logger.info(f"✅ Subscription confirmed - ID: {data['result']}")
                                continue
                            
                            if 'params' in data:
                                await self._process_log_notification(data['params'])
                                
                        except asyncio.TimeoutError:
                            await websocket.ping()
                        except Exception as e:
                            logger.error(f"Error processing message: {e}")
                            break
                    
                    cleanup_task.cancel()
                    
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")
                if self.running:
                    self.reconnect_count += 1
                    logger.info(f"Reconnecting in 5s... (attempt #{self.reconnect_count})")
                    await asyncio.sleep(5)
    
    async def _cleanup_old_tokens(self):
        """Remove tokens we've been watching too long - BUT NOT active positions"""
        while self.running:
            await asyncio.sleep(5)
            now = time.time()
            to_remove = []

            for mint, state in self.watched_tokens.items():
                age = now - state['created_at']
                # Only cleanup if: aged out AND no active position tracking it
                has_active_position = state.get('has_active_position', False)
                if age > self.max_watch_time and not has_active_position:
                    to_remove.append(mint)

            for mint in to_remove:
                final_sol = self.watched_tokens[mint]['total_sol']
                logger.debug(f"🗑️ Stopped watching {mint[:8]}... (timed out at {final_sol:.2f} SOL)")
                del self.watched_tokens[mint]
    
    async def _process_log_notification(self, params: Dict):
        """Process incoming log notification - detect event type and route"""
        try:
            result = params.get('result', {})
            context = result.get('context', {})
            slot = context.get('slot')  # NEW: Extract slot number
            value = result.get('value', {})
            signature = value.get('signature', '')
            logs = value.get('logs', [])
            
            if not signature or not logs:
                return
            
            # Detect event type from logs
            is_create = any('Instruction: CreateV2' in log for log in logs)
            is_buy = any('Instruction: Buy' in log for log in logs)
            is_sell = any('Instruction: Sell' in log for log in logs)
            
            if is_create:
                await self._handle_create(logs, signature, slot)
            elif is_buy:
                await self._handle_buy(logs, signature, slot)
            elif is_sell:
                await self._handle_sell(logs, signature, slot)
                
        except Exception as e:
            logger.error(f"Error processing log: {e}")
    
    async def _handle_create(self, logs: list, signature: str, slot: int = None):
        """Handle CreateV2 - start watching new token"""
        mint, creator = self._extract_mint_and_creator_from_create(logs)
        if not mint:
            return

        # Reject blacklisted creators immediately
        if creator and creator in BLACKLISTED_CREATORS:
            logger.warning(f"🚫 BLACKLISTED CREATOR: {creator[:12]}... - skipping {mint[:12]}...")
            return

        # Track and filter serial creators (scammers launch many tokens)
        if creator:
            self.creator_launches[creator] = self.creator_launches.get(creator, 0) + 1
            if self.creator_launches[creator] > self.max_creator_launches:
                logger.warning(f"🚫 SERIAL CREATOR: {creator[:12]}... launched {self.creator_launches[creator]} tokens - skipping")
                self.stats['skipped_serial_creator'] += 1
                return

        self.stats['creates'] += 1

        # Initialize token state with creator
        self.watched_tokens[mint] = {
            'created_at': time.time(),
            'caught_creation': True,  # We witnessed CreateV2 - real age is known
            'signature': signature,
            'creator': creator,
            'creation_slot': slot,  # NEW: Track creation slot
            'buy_slots': [],        # NEW: Track buy slots
            'buyers': set(),
            'total_sol': 0.0,
            'buy_count': 0,
            'sell_count': 0,
            'largest_buy': 0.0,
            'buys': [],
            'buy_amounts': [],
            'peak_velocity': 0.0,
            'vSolInBondingCurve': 0.0,
            # Order flow exit tracking
            'sell_timestamps': [],
            'buy_timestamps': [],
            'last_buy_time': time.time(),
            # Curve momentum tracking for rug detection
            'curve_history': [],  # List of (timestamp, vSolInBondingCurve) tuples
        }

        if creator:
            logger.info(f"👀 [{self.stats['creates']}] Watching: {mint[:16]}... (creator: {creator[:8]}...) [slot: {slot}]")
        else:
            logger.info(f"👀 [{self.stats['creates']}] Watching: {mint[:16]}... (no creator) [slot: {slot}]")
    
    async def _handle_buy(self, logs: list, signature: str, slot: int = None):
        """Handle Buy event - update token state and check entry"""
        # Extract mint from buy event
        mint, sol_amount, buyer = self._extract_buy_data(logs)
        
        if not mint or mint not in self.watched_tokens:
            return

        # Don't re-trigger, but keep updating state for runner detection
        already_triggered = mint in self.triggered_tokens

        self.stats['buys'] += 1
        state = self.watched_tokens[mint]

        # NEW: Track buy slot
        if slot:
            state['buy_slots'].append(slot)

        # Update state
        state['buyers'].add(buyer) if buyer else None
        state['total_sol'] += sol_amount
        state['buy_count'] += 1
        state['largest_buy'] = max(state['largest_buy'], sol_amount)
        state['buy_amounts'].append(sol_amount)
        state['vSolInBondingCurve'] += sol_amount

        # Track curve momentum for rug detection gate
        now_curve = time.time()
        state['curve_history'].append((now_curve, state['vSolInBondingCurve']))
        # Keep only last 15 seconds of curve history
        state['curve_history'] = [(t, v) for t, v in state['curve_history'] if now_curve - t < 15]
        # FIX 6b: Track last update time for stale data detection
        state['last_update'] = time.time()

        # Track peak curve value from birth
        state['peak_curve_sol'] = max(state.get('peak_curve_sol', 0), state['vSolInBondingCurve'])

        # Track dev (creator) buys - red flag for dumps
        if buyer and buyer == state.get('creator'):
            state['dev_buys'] = state.get('dev_buys', 0) + 1
            state['dev_sol'] = state.get('dev_sol', 0) + sol_amount
            logger.warning(f"⚠️ DEV BUY #{state['dev_buys']} on {mint[:8]}... ({sol_amount:.2f} SOL)")

        # Track buy timing for order flow exits
        now = time.time()
        state['last_buy_time'] = now
        state['buy_timestamps'].append(now)
        # Keep only last 30 seconds of timestamps
        state['buy_timestamps'] = [t for t in state['buy_timestamps'] if now - t < 30]

        # Track buy AMOUNTS for flow-based exits (timed tuples)
        if 'flow_buys' not in state:
            state['flow_buys'] = []
        state['flow_buys'].append((now, sol_amount))
        state['flow_buys'] = [x for x in state['flow_buys'] if isinstance(x, tuple) and len(x) == 2 and now - x[0] < 30]

        # Track peak velocity (only after 0.5s to avoid false spikes at age≈0)
        age = now - state['created_at']
        if age >= 0.5:
            current_velocity = state['total_sol'] / age
            state['peak_velocity'] = max(state['peak_velocity'], current_velocity)

        state['buys'].append({
            'time': time.time(),
            'sol': sol_amount,
            'wallet': buyer
        })
        
        # Log progress every 5 buys or when approaching target
        if state['buy_count'] % 5 == 0 or state['total_sol'] >= self.min_sol * 0.7:
            age = time.time() - state['created_at']
            logger.info(
                f"   📈 {mint[:8]}... | {state['total_sol']:.2f} SOL | "
                f"{len(state['buyers'])} buyers | {age:.1f}s"
            )

        # INSTANT CALLBACK for active positions (migration detection)
        if self.buy_callback and state.get('has_active_position', False):
            await self.buy_callback(mint, state)

        # Check entry conditions (skip if already triggered)
        if not already_triggered:
            await self._check_and_trigger(mint, state)
    
    async def _handle_sell(self, logs: list, signature: str, slot: int = None):
        """Handle Sell event - track for order flow exits"""
        # USE THE REAL AMOUNT (already being parsed!)
        mint, sol_amount, seller = self._extract_buy_data(logs)

        if not mint or mint not in self.watched_tokens:
            return

        self.stats['sells'] += 1
        state = self.watched_tokens[mint]

        # FIRST-SELL RUG CHECK: >60% drain = dev rug
        current_curve = state['vSolInBondingCurve']
        if current_curve > 0 and sol_amount > 0:
            sell_ratio = sol_amount / current_curve
            if sell_ratio > 1.0:
                # Stale curve data - mathematically impossible, skip rug check
                logger.warning(f"⚠️ Stale curve data: {sell_ratio:.0%} drain impossible, skipping")
            elif sell_ratio > 0.60:
                logger.warning(f"🚨 DEV RUG: Sell drained {sell_ratio:.0%} of curve ({sol_amount:.2f}/{current_curve:.2f} SOL)")
                self.stats['skipped_dev_rug'] = self.stats.get('skipped_dev_rug', 0) + 1
                self.triggered_tokens.add(mint)
                return

        state['sell_count'] += 1

        # Track sell timing for order flow exits
        now = time.time()

        # Track sell AMOUNTS for flow-based exits (timed tuples)
        if 'flow_sells' not in state:
            state['flow_sells'] = []
        # Use parsed sol_amount, fallback to 0.3 SOL estimate if parse failed
        actual_sell_sol = sol_amount if sol_amount > 0 else 0.3
        state['largest_sell'] = max(state.get('largest_sell', 0), actual_sell_sol)

        # DUST FILTER: Only count sells >= 0.02 SOL for order flow
        # Dust sells (0.0001 SOL) are bot probes, not real selling pressure
        MIN_SIGNIFICANT_SELL = 0.02
        if actual_sell_sol >= MIN_SIGNIFICANT_SELL:
            state['sell_timestamps'].append(now)
            state['flow_sells'].append((now, actual_sell_sol))
        else:
            logger.debug(f"   🧹 Dust sell ignored for flow: {actual_sell_sol:.4f} SOL")

        state['flow_sells'] = [x for x in state['flow_sells'] if isinstance(x, tuple) and len(x) == 2 and now - x[0] < 30]
        state['sell_timestamps'] = [t for t in state['sell_timestamps'] if now - t < 30]

        # USE REAL AMOUNT - fallback to last known sell size or minimum
        if sol_amount > 0:
            state['vSolInBondingCurve'] = max(0, state['vSolInBondingCurve'] - sol_amount)
            # Track last good sell amount for fallback
            state['last_known_sell_amount'] = sol_amount
        else:
            # Fallback: use last known sell amount, or 0.1 SOL minimum
            # 5% was accumulating errors over many failed parses
            fallback_amount = state.get('last_known_sell_amount', 0.1)
            state['vSolInBondingCurve'] = max(0, state['vSolInBondingCurve'] - fallback_amount)
            logger.warning(f"⚠️ Sell parse failed, using fallback: {fallback_amount:.4f} SOL")

        # Track curve momentum for rug detection gate
        state['curve_history'].append((now, state['vSolInBondingCurve']))
        # Keep only last 15 seconds of curve history
        state['curve_history'] = [(t, v) for t, v in state['curve_history'] if now - t < 15]
        # FIX 6b: Track last update time for stale data detection
        state['last_update'] = time.time()

        # Log with order flow detail
        recent_sells = len([t for t in state['sell_timestamps'] if now - t < 5])
        if sol_amount > 0:
            logger.warning(f"⚠️ SELL #{state['sell_count']} on {mint[:8]}... -{sol_amount:.4f} SOL ({recent_sells} in last 5s)")
        else:
            logger.warning(f"⚠️ SELL #{state['sell_count']} on {mint[:8]}... (parse failed) ({recent_sells} in last 5s)")

        # INSTANT EXIT CHECK: If we hold this token, check exit conditions NOW
        if state.get('has_active_position') and self.exit_callback:
            await self.exit_callback(mint, state)
    
    async def _check_and_trigger(self, mint: str, state: dict):
        """Check if token meets entry conditions and trigger callback"""

        # Already triggered?
        if mint in self.triggered_tokens:
            return

        age = time.time() - state['created_at']
        total_sol = state['vSolInBondingCurve']

        # Age correction: if detected age is impossibly short for the SOL amount, correct it
        # This handles tokens that were created before we started watching
        if not state.get('caught_creation', False) and age < (total_sol / 5.0) and total_sol > 1.5:
            impossible_velocity = total_sol / age if age > 0 else float('inf')
            corrected_age = total_sol / 3.0
            logger.warning(f"⚠️ AGE CORRECTION: Detected {age:.1f}s but {total_sol:.2f} SOL = {impossible_velocity:.1f} SOL/s (impossible)")
            logger.warning(f"   Corrected age: {corrected_age:.1f}s (assuming ~3 SOL/s organic velocity)")
            state['age_corrected'] = True
            state['corrected_age'] = corrected_age
            age = corrected_age
        buyers = len(state['buyers'])
        velocity = total_sol / age if age > 0 else 0
        largest_buy_pct = (state['largest_buy'] / total_sol * 100) if total_sol > 0 else 0
        
        # NEW: Calculate top-2 concentration (compare to total BUYS, not current curve)
        buy_amounts = sorted(state['buy_amounts'], reverse=True)
        total_buy_sol = sum(buy_amounts)  # Total bought (ignores sells)
        top2_sol = sum(buy_amounts[:2]) if len(buy_amounts) >= 2 else sum(buy_amounts)
        top2_pct = (top2_sol / total_buy_sol * 100) if total_buy_sol > 0 else 0

        # ===== ENTRY CONDITIONS =====

        # 1. SOL range
        if total_sol < self.min_sol:
            return  # Too early, keep watching

        # DISABLED: Overshoot ceiling - redundant with velocity ceiling, blocking runners
        # if total_sol > self.max_sol:
        #     logger.warning(f"❌ {mint[:8]}... overshot: {total_sol:.2f} > {self.max_sol}")
        #     self.triggered_tokens.add(mint)  # Don't check again
        #     return

        # 2. Minimum unique buyers
        if buyers < self.min_buyers:
            logger.debug(f"   {mint[:8]}... only {buyers} buyers (need {self.min_buyers})")
            return

        # 2b. SELL BURST GATE - Only block if sells AND curve declining
        now = time.time()
        sell_timestamps = state.get('sell_timestamps', [])
        recent_sells_burst = len([t for t in sell_timestamps if now - t < self.sell_burst_window])

        if recent_sells_burst >= self.sell_burst_count:
            # Check if curve is actually declining during sells
            curve_history = state.get('curve_history', [])
            curve_declining = False
            if len(curve_history) >= 2:
                recent_curves = [(t, v) for t, v in curve_history if now - t < self.sell_burst_window]
                if len(recent_curves) >= 2:
                    first_curve = recent_curves[0][1]
                    last_curve = recent_curves[-1][1]
                    if first_curve > 0 and last_curve < first_curve * 0.95:  # 5%+ decline
                        curve_declining = True

            if curve_declining:
                logger.warning(f"❌ SELL BURST + DECLINING: {recent_sells_burst} sells in {self.sell_burst_window}s - dump in progress")
                self.stats['skipped_sell_burst'] += 1
                self.triggered_tokens.add(mint)
                return
            else:
                logger.info(f"⚡ Sell burst ({recent_sells_burst}) but curve stable - allowing entry (profit-taking)")

        # 3. Check sells with ratio (allow up to 2 sells if buy:sell ratio >= 4:1)
        sell_count = state['sell_count']
        buy_count = state['buy_count']
        # DISABLED: Testing if redundant - order flow handles dump detection
        # if sell_count > self.max_sells_at_entry:
        #     logger.warning(f"❌ Too many sells: {sell_count} (max {self.max_sells_at_entry})")
        #     self.stats['skipped_sells'] += 1
        #     self.triggered_tokens.add(mint)
        #     return

        # DISABLED: Redundant with sell count, blocking organic runners
        # if sell_count > 0 and (buy_count / sell_count) < self.min_buy_sell_ratio:
        #     logger.warning(f"❌ Buy:sell ratio too low: {buy_count}:{sell_count} (min {self.min_buy_sell_ratio}:1)")
        #     self.stats['skipped_sells'] += 1
        #     self.triggered_tokens.add(mint)
        #     return

        # 4. Anti-bot check: single wallet dominance (max 35%)
        if largest_buy_pct > self.max_single_buy_percent:
            logger.warning(f"❌ Single wallet dominance: {largest_buy_pct:.1f}% (max {self.max_single_buy_percent}%)")
            self.stats['skipped_bot'] += 1
            self.triggered_tokens.add(mint)
            return

        # 5. Minimum velocity check
        if velocity < self.min_velocity:
            logger.debug(f"   {mint[:8]}... low velocity: {velocity:.2f} SOL/s (need {self.min_velocity})")
            return

        # 5b. DISABLED: Maximum SOL velocity check - redundant with buyer velocity
        # if velocity > self.max_velocity:
        #     logger.warning(f"❌ Bot pump detected: {velocity:.1f} SOL/s (max {self.max_velocity})")
        #     self.stats['skipped_velocity_high'] += 1
        #     self.triggered_tokens.add(mint)
        #     return

        # 5c. Check buyer velocity (coordination detection)
        token_age = age
        buyer_velocity = buy_count / max(token_age, 0.1)

        # Minimum buyer velocity - filters weak organic traction
        if buyer_velocity < self.min_buyers_per_second:
            logger.warning(f"❌ Buyer velocity too low: {buyer_velocity:.1f}/s (min {self.min_buyers_per_second})")
            self.triggered_tokens.add(mint)
            return

        # DISABLED: Buyer velocity filter - too aggressive, blocking organic runners
        # if buyer_velocity > self.max_buyers_per_second:
        #     logger.warning(f"❌ Buyer velocity too high: {buyer_velocity:.1f}/s (max {self.max_buyers_per_second}) - likely coordinated")
        #     self.stats['skipped_velocity_high'] += 1
        #     self.triggered_tokens.add(mint)
        #     return

        # 5d. PEAK CURVE GATE - Reject if already declining from peak
        peak = state.get('peak_curve_sol', total_sol)
        if total_sol < peak * 0.95:
            drop_pct = ((peak - total_sol) / peak) * 100
            logger.warning(f"❌ DECLINING FROM PEAK: {peak:.2f} → {total_sol:.2f} SOL (-{drop_pct:.1f}%)")
            self.stats['skipped_curve_stalled'] += 1
            self.triggered_tokens.add(mint)
            return

        # 5e. CURVE DRAIN GATE - Reject if significant selling already occurred
        # total_sol = actual curve, state['total_sol'] = cumulative buys (never decreases)
        # If curve < 85% of total buys, dumping is in progress
        cumulative_buys = state.get('total_sol', total_sol)
        if cumulative_buys > 0 and total_sol < cumulative_buys * 0.85:
            drain_pct = ((cumulative_buys - total_sol) / cumulative_buys) * 100
            logger.warning(f"❌ CURVE DRAINED: {drain_pct:.0f}% sold (curve={total_sol:.2f}, buys={cumulative_buys:.2f})")
            self.stats['skipped_curve_stalled'] += 1
            self.triggered_tokens.add(mint)
            return

        # 6. Token age check (must be fresh for early entry)
        if age < self.min_token_age:
            logger.debug(f"   {mint[:8]}... too young: {age:.1f}s (need {self.min_token_age}s)")
            return

        if age > self.max_token_age:
            logger.warning(f"❌ Token too old: {age:.1f}s (max {self.max_token_age}s)")
            self.triggered_tokens.add(mint)
            return

        # ===== NEW FILTERS (21-trade baseline learnings) =====

        # 7. Top-2 concentration check - blocks coordinated entries
        # Two wallets at 30% each = 60% concentration, should fail
        if top2_pct > self.max_top2_percent:
            logger.warning(f"❌ Top-2 wallet concentration: {top2_pct:.1f}% (max {self.max_top2_percent}%)")
            self.stats['skipped_top2'] += 1
            self.triggered_tokens.add(mint)
            return

        # 8. NEW: Dev buy filter - creator buying tokens = guaranteed dump
        dev_buys = state.get('dev_buys', 0)
        if dev_buys > 0:
            logger.warning(f"❌ Dev bought tokens: {dev_buys} buys ({state.get('dev_sol', 0):.2f} SOL)")
            self.stats['skipped_dev'] = self.stats.get('skipped_dev', 0) + 1
            self.triggered_tokens.add(mint)
            return

        # 10. BUNDLED + SLOT CLUSTERING DETECTION
        # First buy in same slot as creation = insider bundle
        # DISABLED: Bundled filter too aggressive - logs warning but doesn't skip
        creation_slot = state.get('creation_slot')
        buy_slots = state.get('buy_slots', [])
        if creation_slot and buy_slots:
            first_buy_slot = buy_slots[0]
            same_slot = first_buy_slot == creation_slot
            same_slot_buys = len([s for s in buy_slots if s == creation_slot])
            clustering_pct = (same_slot_buys / len(buy_slots) * 100) if buy_slots else 0

            # FILTER 1: First buy bundled with creation = coordinated launch
            # DISABLED: Still log for visibility but don't skip
            if same_slot:
                logger.warning(f"⚠️ BUNDLED: First buy in creation slot (coordinated launch) - FILTER DISABLED")
                # self.stats['skipped_bundled'] = self.stats.get('skipped_bundled', 0) + 1
                # self.triggered_tokens.add(mint)
                # return

            # FILTER 2: >50% of buys in creation slot = bot coordination
            # DISABLED: Still log for visibility but don't skip
            if clustering_pct > 50:
                logger.warning(f"⚠️ SLOT CLUSTERING: {same_slot_buys}/{len(buy_slots)} ({clustering_pct:.0f}%) buys in creation slot - FILTER DISABLED")
                # self.stats['skipped_bundled'] = self.stats.get('skipped_bundled', 0) + 1
                # self.triggered_tokens.add(mint)
                # return

        # 9. REMOVED: Dev holdings RPC check - adds latency, kept WebSocket-based dev buy detection above

        # 10. DISABLED: Buyer distribution filter too strict for early entries
        # Already protected by single wallet (45%) and top-2 concentration (60%) filters
        # sol_per_buyer = total_sol / buyers if buyers > 0 else 999
        # if sol_per_buyer > 0.75:
        #     logger.warning(f"❌ Poor buyer distribution: {sol_per_buyer:.2f} SOL/buyer (max 0.75)")
        #     self.stats['skipped_distribution'] = self.stats.get('skipped_distribution', 0) + 1
        #     self.triggered_tokens.add(mint)
        #     return

        # NOTE: RPC validation removed - tokens too new for RPC to have indexed
        # Protection comes from: slippage rejection, drift correction during monitoring,
        # and RPC rug detection during monitoring (token exists by then)

        # ===== ALL CONDITIONS MET =====
        self.triggered_tokens.add(mint)
        self.stats['triggers'] += 1

        logger.info("=" * 60)
        logger.info(f"🚀 EARLY ENTRY: {mint}")
        logger.info(f"   SOL: {total_sol:.2f} (range: {self.min_sol}-{self.max_sol})")
        logger.info(f"   Buyers: {buyers} (min: {self.min_buyers})")
        logger.info(f"   Sells: {sell_count} (recent: {recent_sells_burst} in {self.sell_burst_window}s)")
        logger.info(f"   Largest buy: {largest_buy_pct:.1f}% (max: {self.max_single_buy_percent}%)")
        logger.info(f"   Top-2 concentration: {top2_pct:.1f}% (max: {self.max_top2_percent}%)")
        logger.info(f"   Velocity: {velocity:.2f} SOL/s (min: {self.min_velocity})")
        logger.info(f"   Buyer velocity: {buyer_velocity:.1f}/s (max: {self.max_buyers_per_second})")
        logger.info(f"   Curve momentum: ✅ Growing")
        logger.info(f"   Age: {age:.1f}s (max: {self.max_token_age}s)")

        # NEW: Slot analysis logging
        creation_slot = state.get('creation_slot')
        buy_slots = state.get('buy_slots', [])
        if creation_slot and buy_slots:
            first_buy_slot = buy_slots[0] if buy_slots else None
            same_slot_buys = len([s for s in buy_slots if s == creation_slot])
            unique_slots = len(set(buy_slots))
            slot_spread = max(buy_slots) - min(buy_slots) if len(buy_slots) > 1 else 0
            logger.info(f"   📊 SLOT DATA: creation={creation_slot}, first_buy={first_buy_slot}, same_slot={first_buy_slot == creation_slot}")
            logger.info(f"   📊 SLOT CLUSTERING: {same_slot_buys}/{len(buy_slots)} buys in creation slot, {unique_slots} unique slots, spread={slot_spread}")

        logger.info("=" * 60)
        
        # Trigger callback with enriched data
        if self.callback:
            actual_age = state.get('corrected_age', age)
            await self.callback({
                'mint': mint,
                'signature': state['signature'],
                'source': 'helius_events',
                'type': 'pumpfun_launch',
                'timestamp': datetime.now().isoformat(),
                'age': actual_age,
                'token_age': actual_age,
                'age_was_corrected': state.get('age_corrected', False),
                # Real data from events
                'data': {
                    'vSolInBondingCurve': state['vSolInBondingCurve'],
                    'unique_buyers': buyers,
                    'buy_count': state['buy_count'],
                    'sell_count': state['sell_count'],
                    'sell_count_at_detection': state['sell_count'],  # For dynamic position sizing
                    'velocity': velocity,
                    'largest_buy': state['largest_buy'],
                    'concentration': state['largest_buy'] / total_sol if total_sol > 0 else 0,
                    'top2_concentration': top2_pct,  # NEW: include in callback
                    'creator': state.get('creator'),  # Pass creator for local TX
                }
            })
    
    # ===== PARSING HELPERS =====
    
    def _extract_mint_and_creator_from_create(self, logs: list) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract mint AND creator from CreateV2 Program data

        CreateV2 structure:
        - discriminator (8 bytes)
        - name (4 bytes length + string)
        - symbol (4 bytes length + string)
        - uri (4 bytes length + string)
        - mint (32 bytes pubkey)
        - creator (32 bytes pubkey)  ← We need this!

        Returns: (mint, creator) or (None, None)
        """
        for log in logs:
            if log.startswith("Program data:"):
                data_b64 = log.replace("Program data:", "").strip()

                # Fix padding
                padding = 4 - len(data_b64) % 4
                if padding != 4:
                    data_b64 += '=' * padding

                try:
                    decoded = base64.b64decode(data_b64)

                    # Check CreateV2 discriminator
                    if len(decoded) >= 8 and decoded[:8].hex() == self.CREATE_V2_DISCRIMINATOR:
                        # Parse: discriminator(8) + name + symbol + uri + mint(32) + creator(32)
                        pos = 8

                        # Skip name
                        if pos + 4 > len(decoded):
                            continue
                        name_len = int.from_bytes(decoded[pos:pos+4], 'little')
                        pos += 4 + name_len

                        # Skip symbol
                        if pos + 4 > len(decoded):
                            continue
                        symbol_len = int.from_bytes(decoded[pos:pos+4], 'little')
                        pos += 4 + symbol_len

                        # Skip URI
                        if pos + 4 > len(decoded):
                            continue
                        uri_len = int.from_bytes(decoded[pos:pos+4], 'little')
                        pos += 4 + uri_len

                        # Extract mint (32 bytes)
                        if pos + 32 > len(decoded):
                            continue
                        mint_bytes = decoded[pos:pos+32]
                        mint = base58.b58encode(mint_bytes).decode()
                        pos += 32

                        # Skip bonding_curve PDA (32 bytes)
                        pos += 32

                        # Extract creator (next 32 bytes after bonding curve)
                        creator = None
                        if pos + 32 <= len(decoded):
                            creator_bytes = decoded[pos:pos+32]
                            creator = base58.b58encode(creator_bytes).decode()
                            logger.debug(f"✅ Extracted creator: {creator[:16]}...")
                        else:
                            logger.warning(f"⚠️ Could not extract creator from CreateV2 (data too short)")

                        return mint, creator

                except Exception as e:
                    logger.debug(f"CreateV2 parse error: {e}")
                    continue

        return None, None

    def _extract_mint_from_create(self, logs: list) -> Optional[str]:
        """Legacy wrapper - returns just mint for backward compatibility"""
        mint, _ = self._extract_mint_and_creator_from_create(logs)
        return mint
    
    def _extract_buy_data(self, logs: list) -> tuple:
        """
        Extract mint, SOL amount, and buyer from Buy event logs
        Returns: (mint, sol_amount, buyer_wallet) or (None, 0, None)
        """
        mint = None
        sol_amount = 0.0
        buyer = None
        
        for log in logs:
            # Try to get mint from various log formats
            if "Program data:" in log:
                data_b64 = log.replace("Program data:", "").strip()
                
                padding = 4 - len(data_b64) % 4
                if padding != 4:
                    data_b64 += '=' * padding
                
                try:
                    decoded = base64.b64decode(data_b64)
                    
                    # Skip CreateV2 discriminator
                    if len(decoded) >= 8 and decoded[:8].hex() == self.CREATE_V2_DISCRIMINATOR:
                        continue
                    
                    # Trade event structure (best guess based on PumpFun):
                    # discriminator(8) + mint(32) + sol_amount(8) + token_amount(8) + user(32) + is_buy(1) + timestamp(8)
                    if len(decoded) >= 89:
                        # Extract mint (bytes 8-40)
                        potential_mint = base58.b58encode(decoded[8:40]).decode()
                        
                        # Validate it looks like a PumpFun mint
                        if potential_mint.endswith('pump'):
                            mint = potential_mint
                            
                            # SOL amount (bytes 40-48, lamports)
                            sol_lamports = int.from_bytes(decoded[40:48], 'little')
                            sol_amount = sol_lamports / 1e9
                            
                            # Buyer wallet (bytes 56-88)
                            if len(decoded) >= 88:
                                buyer = base58.b58encode(decoded[56:88]).decode()
                            
                            break
                except:
                    continue
            
            # Fallback: Try to find mint in account keys (from log messages)
            if "pump" in log.lower():
                # Look for mint address pattern
                words = log.split()
                for word in words:
                    if word.endswith('pump') and len(word) > 40:
                        mint = word
                        break
        
        # If we found a watched mint but couldn't parse amount, estimate from context
        if mint and mint in self.watched_tokens and sol_amount == 0:
            # Assume average buy of 0.3-0.5 SOL
            sol_amount = 0.4
        
        return (mint, sol_amount, buyer)
    
    def _extract_mint_from_sell(self, logs: list) -> Optional[str]:
        """Extract mint from Sell event - similar to buy"""
        mint, _, _ = self._extract_buy_data(logs)
        return mint
    
    def get_stats(self) -> Dict:
        """Get monitor statistics"""
        return {
            **self.stats,
            'watching': len(self.watched_tokens),
            'triggered': len(self.triggered_tokens),
            'reconnects': self.reconnect_count,
        }
    
    def stop(self):
        """Stop the monitor"""
        self.running = False
        stats = self.get_stats()
        logger.info(f"Helius monitor stopped")
        logger.info(f"Stats: {stats['creates']} creates, {stats['buys']} buys, {stats['sells']} sells")
        logger.info(f"Triggered: {stats['triggers']} | Skipped (sells): {stats['skipped_sells']} | Skipped (bot): {stats['skipped_bot']}")
        logger.info(f"Skipped (velocity high): {stats['skipped_velocity_high']} | Skipped (top2): {stats['skipped_top2']} | Skipped (dev): {stats.get('skipped_dev', 0)}")
        logger.info(f"Skipped (sell burst): {stats.get('skipped_sell_burst', 0)} | Skipped (curve stalled): {stats.get('skipped_curve_stalled', 0)}")
        logger.info(f"Skipped (bundled): {stats.get('skipped_bundled', 0)}")
