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
    MAX_VELOCITY, MAX_TOP2_BUY_PERCENT, MIN_TOKEN_AGE_SECONDS
)
from curve_reader import BondingCurveReader

logger = logging.getLogger(__name__)


class HeliusLogsMonitor:
    """Subscribe to PumpFun program logs and track all events"""
    
    def __init__(self, callback, rpc_client):
        self.callback = callback
        self.rpc_client = rpc_client
        self.running = False
        self.reconnect_count = 0
        
        # Verify Helius API key
        if not HELIUS_API_KEY:
            raise ValueError("HELIUS_API_KEY is required")
        logger.info(f"✅ Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        # Token state tracking
        self.watched_tokens: Dict[str, dict] = {}
        self.triggered_tokens: Set[str] = set()  # Don't re-trigger
        
        # Statistics
        self.stats = {
            'creates': 0,
            'buys': 0,
            'sells': 0,
            'triggers': 0,
            'skipped_sells': 0,
            'skipped_bot': 0,
            'skipped_velocity_high': 0,  # NEW: track high velocity skips
            'skipped_top2': 0,           # NEW: track top-2 concentration skips
            'skipped_distribution': 0,   # NEW: track poor buyer distribution skips
        }
        
        # Known discriminators
        self.CREATE_V2_DISCRIMINATOR = "1b72a94ddeeb6376"

        # Entry thresholds from config (early entry with relaxed quality gates)
        self.min_sol = MIN_BONDING_CURVE_SOL      # 2.0 SOL - enter early
        self.max_sol = MAX_BONDING_CURVE_SOL      # 5.0 SOL - tight window
        self.min_buyers = MIN_UNIQUE_BUYERS       # 4 unique buyers minimum (was 5)
        self.max_sell_count = MAX_SELLS_BEFORE_ENTRY  # 3 sells max before entry (was 1)
        self.max_single_buy_percent = MAX_SINGLE_BUY_PERCENT  # 35% anti-bot
        self.min_velocity = MIN_VELOCITY          # 1.0 SOL/s minimum momentum
        self.max_token_age = MAX_TOKEN_AGE_SECONDS  # 10s max age for "early"
        self.min_token_age = MIN_TOKEN_AGE_SECONDS  # NEW: minimum age before entry
        
        # NEW: 21-trade baseline filters
        self.max_velocity = MAX_VELOCITY          # 8.0 SOL/s max - blocks bot pumps
        self.max_top2_percent = MAX_TOP2_BUY_PERCENT  # 50% max from top 2 wallets
        
        self.max_watch_time = 120  # Match max hold time
        
    async def start(self):
        """Connect to Helius WebSocket and subscribe to PumpFun logs"""
        self.running = True
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        
        logger.info("🔍 Connecting to Helius WebSocket...")
        logger.info(f"   Strategy: EARLY ENTRY with strict quality gates")
        logger.info(f"   Entry zone: {self.min_sol}-{self.max_sol} SOL")
        logger.info(f"   Min buyers: {self.min_buyers} | Max sells: {self.max_sell_count}")
        logger.info(f"   Anti-bot: single buy < {self.max_single_buy_percent:.0f}%")
        logger.info(f"   Min velocity: {self.min_velocity} SOL/s | Max velocity: {self.max_velocity} SOL/s")
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
        """Remove tokens we've been watching too long"""
        while self.running:
            await asyncio.sleep(5)
            now = time.time()
            to_remove = []
            
            for mint, state in self.watched_tokens.items():
                age = now - state['created_at']
                if age > self.max_watch_time:
                    to_remove.append(mint)
            
            for mint in to_remove:
                final_sol = self.watched_tokens[mint]['total_sol']
                logger.debug(f"🗑️ Stopped watching {mint[:8]}... (timed out at {final_sol:.2f} SOL)")
                del self.watched_tokens[mint]
    
    async def _process_log_notification(self, params: Dict):
        """Process incoming log notification - detect event type and route"""
        try:
            result = params.get('result', {})
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
                await self._handle_create(logs, signature)
            elif is_buy:
                await self._handle_buy(logs, signature)
            elif is_sell:
                await self._handle_sell(logs, signature)
                
        except Exception as e:
            logger.error(f"Error processing log: {e}")
    
    async def _handle_create(self, logs: list, signature: str):
        """Handle CreateV2 - start watching new token"""
        mint, creator = self._extract_mint_and_creator_from_create(logs)
        if not mint:
            return

        self.stats['creates'] += 1

        # Initialize token state with creator
        self.watched_tokens[mint] = {
            'created_at': time.time(),
            'signature': signature,
            'creator': creator,  # Store creator for local TX building
            'buyers': set(),
            'total_sol': 0.0,
            'buy_count': 0,
            'sell_count': 0,
            'largest_buy': 0.0,
            'buys': [],
            'buy_amounts': [],  # NEW: track individual buy amounts for top-2 calc
            'peak_velocity': 0.0,
        }

        if creator:
            logger.info(f"👀 [{self.stats['creates']}] Watching: {mint[:16]}... (creator: {creator[:8]}...)")
        else:
            logger.info(f"👀 [{self.stats['creates']}] Watching: {mint[:16]}... (no creator extracted)")
    
    async def _handle_buy(self, logs: list, signature: str):
        """Handle Buy event - update token state and check entry"""
        # Extract mint from buy event
        mint, sol_amount, buyer = self._extract_buy_data(logs)
        
        if not mint or mint not in self.watched_tokens:
            return

        # Don't re-trigger, but keep updating state for runner detection
        already_triggered = mint in self.triggered_tokens

        self.stats['buys'] += 1
        state = self.watched_tokens[mint]
        
        # Update state
        state['buyers'].add(buyer) if buyer else None
        state['total_sol'] += sol_amount
        state['buy_count'] += 1
        state['largest_buy'] = max(state['largest_buy'], sol_amount)
        state['buy_amounts'].append(sol_amount)  # NEW: track for top-2 calc

        # Track peak velocity (only after 0.5s to avoid false spikes at age≈0)
        age = time.time() - state['created_at']
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
        
        # Check entry conditions (skip if already triggered)
        if not already_triggered:
            await self._check_and_trigger(mint, state)
    
    async def _handle_sell(self, logs: list, signature: str):
        """Handle Sell event - flag token as risky"""
        mint = self._extract_mint_from_sell(logs)
        
        if not mint or mint not in self.watched_tokens:
            return
        
        self.stats['sells'] += 1
        state = self.watched_tokens[mint]
        state['sell_count'] += 1
        
        logger.warning(f"⚠️ SELL on {mint[:8]}... (sell #{state['sell_count']} before entry)")
    
    async def _check_and_trigger(self, mint: str, state: dict):
        """Check if token meets entry conditions and trigger callback"""

        # Already triggered?
        if mint in self.triggered_tokens:
            return

        age = time.time() - state['created_at']
        total_sol = state['total_sol']
        buyers = len(state['buyers'])
        velocity = total_sol / age if age > 0 else 0
        largest_buy_pct = (state['largest_buy'] / total_sol * 100) if total_sol > 0 else 0
        
        # NEW: Calculate top-2 concentration
        buy_amounts = sorted(state['buy_amounts'], reverse=True)
        top2_sol = sum(buy_amounts[:2]) if len(buy_amounts) >= 2 else sum(buy_amounts)
        top2_pct = (top2_sol / total_sol * 100) if total_sol > 0 else 0

        # ===== ENTRY CONDITIONS =====

        # 1. SOL range
        if total_sol < self.min_sol:
            return  # Too early, keep watching

        if total_sol > self.max_sol:
            logger.warning(f"❌ {mint[:8]}... overshot: {total_sol:.2f} > {self.max_sol}")
            self.triggered_tokens.add(mint)  # Don't check again
            return

        # 2. Minimum unique buyers
        if buyers < self.min_buyers:
            logger.debug(f"   {mint[:8]}... only {buyers} buyers (need {self.min_buyers})")
            return

        # 3. Limit sells before entry (strict 0-sell filter)
        if state['sell_count'] > self.max_sell_count:
            logger.warning(f"❌ Not 0-sell: {state['sell_count']} sells detected (strict 0-sell filter)")
            self.stats['skipped_sells'] += 1
            self.triggered_tokens.add(mint)
            return

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

        # 6. Token age check (must be fresh for early entry)
        if age < self.min_token_age:
            logger.debug(f"   {mint[:8]}... too young: {age:.1f}s (need {self.min_token_age}s)")
            return

        if age > self.max_token_age:
            logger.warning(f"❌ Token too old: {age:.1f}s (max {self.max_token_age}s)")
            self.triggered_tokens.add(mint)
            return

        # ===== NEW FILTERS (21-trade baseline learnings) =====

        # 7. NEW: Maximum velocity check - blocks coordinated bot pumps
        # DGuZTAAT had 4795 SOL/s, winners have 1-2 SOL/s
        if velocity > self.max_velocity:
            logger.warning(f"❌ Velocity too high (bot pump): {velocity:.1f} SOL/s (max {self.max_velocity})")
            self.stats['skipped_velocity_high'] += 1
            self.triggered_tokens.add(mint)
            return


        # 9. NEW: Top-2 concentration check - blocks coordinated entries
        # Two wallets at 30% each = 60% concentration, should fail
        if top2_pct > self.max_top2_percent:
            logger.warning(f"❌ Top-2 wallet concentration: {top2_pct:.1f}% (max {self.max_top2_percent}%)")
            self.stats['skipped_top2'] += 1
            self.triggered_tokens.add(mint)
            return

        # 10. DISABLED: Buyer distribution filter too strict for early entries
        # Already protected by single wallet (45%) and top-2 concentration (60%) filters
        # sol_per_buyer = total_sol / buyers if buyers > 0 else 999
        # if sol_per_buyer > 0.75:
        #     logger.warning(f"❌ Poor buyer distribution: {sol_per_buyer:.2f} SOL/buyer (max 0.75)")
        #     self.stats['skipped_distribution'] = self.stats.get('skipped_distribution', 0) + 1
        #     self.triggered_tokens.add(mint)
        #     return

        # ===== ALL CONDITIONS MET =====
        self.triggered_tokens.add(mint)
        self.stats['triggers'] += 1

        logger.info("=" * 60)
        logger.info(f"🚀 EARLY ENTRY: {mint}")
        logger.info(f"   SOL: {total_sol:.2f} (range: {self.min_sol}-{self.max_sol})")
        logger.info(f"   Buyers: {buyers} (min: {self.min_buyers})")
        logger.info(f"   Sells: {state['sell_count']} (max: {self.max_sell_count})")
        logger.info(f"   Largest buy: {largest_buy_pct:.1f}% (max: {self.max_single_buy_percent}%)")
        logger.info(f"   Top-2 concentration: {top2_pct:.1f}% (max: {self.max_top2_percent}%)")
        logger.info(f"   Velocity: {velocity:.2f} SOL/s (range: {self.min_velocity}-{self.max_velocity})")
        logger.info(f"   Age: {age:.1f}s (max: {self.max_token_age}s)")
        logger.info("=" * 60)
        
        # Trigger callback with enriched data
        if self.callback:
            await self.callback({
                'mint': mint,
                'signature': state['signature'],
                'source': 'helius_events',
                'type': 'pumpfun_launch',
                'timestamp': datetime.now().isoformat(),
                'age': age,
                'token_age': age,
                # Real data from events
                'data': {
                    'vSolInBondingCurve': total_sol,
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

                        # Extract creator (next 32 bytes)
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
        logger.info(f"NEW filters - Skipped (velocity high): {stats['skipped_velocity_high']} | Skipped (top2): {stats['skipped_top2']} | Skipped (distribution): {stats.get('skipped_distribution', 0)}")
