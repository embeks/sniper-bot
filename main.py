"""
Main Orchestrator - Coordinates Phase 1 sniper bot operations
"""

import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from config import (
    LOG_LEVEL, LOG_FORMAT, LOG_FILE,
    BUY_AMOUNT_SOL, MAX_POSITIONS, MIN_SOL_BALANCE,
    STOP_LOSS_PERCENTAGE, TAKE_PROFIT_PERCENTAGE,
    SELL_DELAY_SECONDS, MAX_POSITION_AGE_SECONDS,
    DRY_RUN, DEBUG_MODE, ENABLE_TELEGRAM_NOTIFICATIONS,
    BLACKLISTED_TOKENS, NOTIFY_PROFIT_THRESHOLD
)

from wallet import WalletManager
from dex import PumpFunDEX
from pumpfun_scanner import PumpFunScanner
from telegram_bot import TelegramBot

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler()]  # Console output only
)

logger = logging.getLogger(__name__)

class Position:
    """Track an active position"""
    def __init__(self, mint: str, amount_sol: float, tokens: float = 0):
        self.mint = mint
        self.amount_sol = amount_sol
        self.tokens = tokens
        self.entry_time = time.time()
        self.entry_price = 0
        self.current_price = 0
        self.pnl_percent = 0
        self.pnl_usd = 0
        self.status = 'pending'
        self.buy_signature = None
        self.sell_signature = None

class SniperBot:
    """Main sniper bot orchestrator"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING PHASE 1 SNIPER BOT")
        logger.info("=" * 60)
        
        # Initialize components
        self.wallet = WalletManager()
        self.dex = PumpFunDEX(self.wallet)
        self.scanner = None
        self.telegram = None
        
        # Track positions
        self.positions: Dict[str, Position] = {}
        self.total_trades = 0
        self.profitable_trades = 0
        self.total_pnl = 0
        self.MAX_POSITIONS = MAX_POSITIONS  # For telegram access
        
        # Control flags
        self.running = False
        self.paused = False  # For pause/resume functionality
        
        # Initialize Telegram bot
        if ENABLE_TELEGRAM_NOTIFICATIONS:
            self.telegram = TelegramBot(self)
        
        # Log initial status
        self._log_startup_info()
    
    def _log_startup_info(self):
        """Log startup information"""
        sol_balance = self.wallet.get_sol_balance()
        tradeable_balance = sol_balance - MIN_SOL_BALANCE
        max_trades = int(tradeable_balance / BUY_AMOUNT_SOL) if tradeable_balance > 0 else 0
        actual_trades = min(max_trades, MAX_POSITIONS) if max_trades > 0 else 0
        
        logger.info(f"📊 STARTUP STATUS:")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
        logger.info("=" * 60)
    
    async def on_token_found(self, token_data: Dict):
        """Handle new token found by monitor"""
        try:
            mint = token_data['mint']
            
            # Check if paused
            if self.paused:
                logger.debug("Bot is paused, skipping token")
                return
            
            # Check if blacklisted
            if mint in BLACKLISTED_TOKENS:
                logger.debug(f"Token {mint[:8]}... is blacklisted")
                return
            
            # Check if we can take a new position
            if len(self.positions) >= MAX_POSITIONS:
                logger.warning(f"❌ Max positions reached ({MAX_POSITIONS})")
                return
            
            if mint in self.positions:
                logger.debug(f"Already have position in {mint[:8]}...")
                return
            
            if not self.wallet.can_trade():
                logger.warning(f"❌ Insufficient balance for trading")
                return
            
            # Log discovery (reduced spam)
            logger.info(f"🎯 Evaluating new token: {mint}")
            
            # Execute buy
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would buy {mint[:8]}... for {BUY_AMOUNT_SOL} SOL")
                signature = "dry_run_sig_" + mint[:10]
            else:
                signature = self.dex.execute_buy(mint)
            
            if signature:
                # Create position
                position = Position(mint, BUY_AMOUNT_SOL)
                position.buy_signature = signature
                position.status = 'active'
                self.positions[mint] = position
                self.total_trades += 1
                
                logger.info(f"✅ Position opened: {mint[:8]}...")
                logger.info(f"   Active positions: {len(self.positions)}")
                
                # Send Telegram notification
                if self.telegram:
                    await self.telegram.notify_buy(mint, BUY_AMOUNT_SOL, signature)
                
                # Schedule monitoring
                asyncio.create_task(self._monitor_position(mint))
                
        except Exception as e:
            logger.error(f"Failed to process token {token_data.get('mint', 'unknown')[:8]}: {e}")
    
    async def _monitor_position(self, mint: str):
        """Monitor a position for exit conditions"""
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            # Wait initial delay before allowing sells
            await asyncio.sleep(SELL_DELAY_SECONDS)
            
            while position.status == 'active' and self.running:
                # Check position age
                age = time.time() - position.entry_time
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ Position {mint[:8]}... exceeded max age")
                    await self._close_position(mint, reason="max_age")
                    break
                
                # Get current price from bonding curve
                curve_data = self.dex.get_bonding_curve_data(mint)
                if not curve_data:
                    logger.warning(f"Lost bonding curve for {mint[:8]}... (may have migrated)")
                    await self._close_position(mint, reason="migration")
                    break
                
                # Calculate P&L (simplified)
                if curve_data['virtual_sol_reserves'] > 0 and curve_data['virtual_token_reserves'] > 0:
                    current_price = curve_data['virtual_sol_reserves'] / curve_data['virtual_token_reserves']
                    
                    # Estimate P&L (this is simplified - real implementation needs actual entry price)
                    if position.entry_price == 0:
                        position.entry_price = current_price  # Set initial price
                    
                    price_change = ((current_price / position.entry_price) - 1) * 100 if position.entry_price > 0 else 0
                    position.pnl_percent = price_change
                    
                    # Check exit conditions
                    if price_change >= TAKE_PROFIT_PERCENTAGE:
                        logger.info(f"💰 Take profit triggered for {mint[:8]}... (+{price_change:.1f}%)")
                        await self._close_position(mint, reason="take_profit")
                        break
                    
                    elif price_change <= -STOP_LOSS_PERCENTAGE:
                        logger.warning(f"🛑 Stop loss triggered for {mint[:8]}... ({price_change:.1f}%)")
                        await self._close_position(mint, reason="stop_loss")
                        break
                
                # Wait before next check
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.error(f"Error monitoring position {mint[:8]}: {e}")
    
    async def _close_position(self, mint: str, reason: str = "manual"):
        """Close a position"""
        try:
            position = self.positions.get(mint)
            if not position or position.status != 'active':
                return
            
            # Get token balance
            token_balance = self.wallet.get_token_balance(mint)
            if token_balance <= 0:
                logger.warning(f"No tokens to sell for {mint[:8]}...")
                position.status = 'closed'
                return
            
            # Execute sell
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would sell {token_balance:,.0f} tokens of {mint[:8]}...")
                signature = "dry_run_sell_" + mint[:10]
            else:
                # Convert to raw amount (assuming 6 decimals for PumpFun tokens)
                token_amount_raw = int(token_balance * 1e6)
                signature = self.dex.execute_sell(mint, token_amount_raw)
            
            if signature:
                position.sell_signature = signature
                position.status = 'closed'
                
                # Update stats
                if position.pnl_percent > 0:
                    self.profitable_trades += 1
                
                self.total_pnl += position.pnl_percent
                
                logger.info(f"📈 Position closed: {mint[:8]}...")
                logger.info(f"   Reason: {reason}")
                logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
                
                # Send Telegram notification
                if self.telegram:
                    sol_price = 250  # Approximate
                    pnl_usd = (position.amount_sol * position.pnl_percent / 100) * sol_price
                    await self.telegram.notify_sell(mint, position.pnl_percent, pnl_usd, reason)
                    
                    # Check for profit milestone
                    if self.total_pnl > NOTIFY_PROFIT_THRESHOLD:
                        total_pnl_sol = self.total_pnl / 100 * BUY_AMOUNT_SOL * self.total_trades
                        await self.telegram.notify_profit_milestone(total_pnl_sol, total_pnl_sol * sol_price)
                
            # Remove from active positions
            del self.positions[mint]
            
        except Exception as e:
            logger.error(f"Failed to close position {mint[:8]}: {e}")
    
    async def run(self):
        """Main run loop"""
        self.running = True
        
        try:
            # Send startup notification
            if self.telegram:
                await self.telegram.send_message("🚀 Bot started successfully\nType /help for commands")
            
            # Start scanner
            self.scanner = PumpFunScanner(self.on_token_found)
            scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Bot is running. Press Ctrl+C to stop.")
            
            # Keep running (no periodic stats logging)
            await scanner_task
            
        except KeyboardInterrupt:
            logger.info("\n🛑 Shutting down...")
            await self.shutdown()
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            if self.telegram:
                await self.telegram.send_message(f"❌ Bot crashed: {e}")
            await self.shutdown()
    
    def _log_stats(self):
        """Log current statistics (only when requested)"""
        try:
            sol_balance = self.wallet.get_sol_balance()
            win_rate = (self.profitable_trades / self.total_trades * 100) if self.total_trades > 0 else 0
            avg_pnl = (self.total_pnl / self.total_trades) if self.total_trades > 0 else 0
            
            logger.info("=" * 50)
            logger.info("📊 STATISTICS")
            logger.info(f"SOL Balance: {sol_balance:.4f}")
            logger.info(f"Active Positions: {len(self.positions)}/{MAX_POSITIONS}")
            logger.info(f"Total Trades: {self.total_trades}")
            logger.info(f"Win Rate: {win_rate:.1f}%")
            logger.info(f"Average P&L: {avg_pnl:+.1f}%")
            logger.info("=" * 50)
            
        except Exception as e:
            logger.error(f"Failed to log stats: {e}")
    
    async def shutdown(self):
        """Clean shutdown"""
        self.running = False
        
        # Send shutdown notification
        if self.telegram:
            await self.telegram.send_message("🛑 Bot shutting down...")
        
        # Close all positions
        logger.info("Closing all positions...")
        for mint in list(self.positions.keys()):
            await self._close_position(mint, reason="shutdown")
        
        # Stop scanner
        if self.scanner:
            self.scanner.stop()
        
        # Stop Telegram bot
        if self.telegram:
            self.telegram.stop()
        
        # Final stats
        self._log_stats()
        logger.info("✅ Shutdown complete")

async def main():
    """Main entry point"""
    bot = SniperBot()
    await bot.run()

if __name__ == "__main__":
    # Handle signals
    def signal_handler(sig, frame):
        logger.info("\nReceived interrupt signal")
        asyncio.get_event_loop().stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # Run bot
    asyncio.run(main())
