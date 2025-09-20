"""
Telegram Bot Integration - Command-based interface for bot control
"""

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import aiohttp

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    ENABLE_TELEGRAM_NOTIFICATIONS, NOTIFY_PROFIT_THRESHOLD
)

logger = logging.getLogger(__name__)

class TelegramBot:
    """Telegram bot for monitoring and control"""
    
    def __init__(self, sniper_bot):
        """Initialize Telegram bot"""
        self.bot = sniper_bot
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self.running = False
        
        # Command handlers
        self.commands = {
            '/start': self.cmd_start,
            '/stop': self.cmd_stop,
            '/pause': self.cmd_pause,
            '/resume': self.cmd_resume,
            '/status': self.cmd_status,
            '/wallet': self.cmd_wallet,
            '/positions': self.cmd_positions,
            '/stats': self.cmd_stats,
            '/pnl': self.cmd_pnl,
            '/config': self.cmd_config,
            '/help': self.cmd_help,
            '/force_sell': self.cmd_force_sell,
            '/blacklist': self.cmd_blacklist,
            '/logs': self.cmd_recent_logs,
            '/set_sl': self.cmd_set_stop_loss,
            '/set_tp': self.cmd_set_take_profit,
        }
        
        if ENABLE_TELEGRAM_NOTIFICATIONS:
            logger.info("✅ Telegram bot initialized")
            asyncio.create_task(self.start_polling())
    
    async def send_message(self, text: str, parse_mode: str = "Markdown"):
        """Send message to Telegram"""
        try:
            if not ENABLE_TELEGRAM_NOTIFICATIONS:
                return
            
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    'chat_id': self.chat_id,
                    'text': text[:4096],  # Telegram limit
                    'parse_mode': parse_mode
                }
                
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to send Telegram message: {await resp.text()}")
                        
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
    
    async def start_polling(self):
        """Poll for commands"""
        self.running = True
        logger.info("📱 Telegram polling started")
        
        while self.running:
            try:
                await self.get_updates()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)
    
    async def get_updates(self):
        """Get and process updates"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/getUpdates"
                params = {
                    'offset': self.last_update_id + 1,
                    'timeout': 30
                }
                
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        for update in data.get('result', []):
                            self.last_update_id = update['update_id']
                            await self.process_update(update)
                            
        except Exception as e:
            logger.debug(f"Update polling error: {e}")
    
    async def process_update(self, update: Dict):
        """Process a Telegram update"""
        try:
            message = update.get('message', {})
            text = message.get('text', '')
            
            if not text:
                return
            
            # Log received command for debugging
            logger.info(f"Telegram command received: {text}")
            
            # Parse command
            parts = text.split()
            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []
            
            # Execute command
            if command in self.commands:
                handler = self.commands[command]
                await handler(args)
            elif text.startswith('/'):
                await self.send_message("❌ Unknown command. Type /help for available commands.")
                
        except Exception as e:
            logger.error(f"Failed to process update: {e}")
            await self.send_message(f"❌ Error processing command: {e}")
    
    # ============================================
    # COMMAND HANDLERS
    # ============================================
    
    async def cmd_start(self, args):
        """Start the bot"""
        if getattr(self.bot, 'running', False):
            await self.send_message("✅ Bot is already running")
        else:
            await self.send_message("🚀 Starting bot...")
            
            # Set running flag
            self.bot.running = True
            
            # Restart scanner if it exists
            if hasattr(self.bot, 'scanner') and self.bot.scanner:
                try:
                    # Cancel existing scanner task if it exists
                    if hasattr(self.bot, 'scanner_task'):
                        self.bot.scanner_task.cancel()
                    
                    # Start new scanner task
                    self.bot.scanner_task = asyncio.create_task(self.bot.scanner.monitor_launches())
                    await self.send_message("✅ Bot started successfully - monitoring for launches")
                except Exception as e:
                    logger.error(f"Failed to restart scanner: {e}")
                    await self.send_message(f"⚠️ Bot started but scanner restart failed: {e}")
            else:
                await self.send_message("✅ Bot started (scanner not initialized)")
    
    async def cmd_stop(self, args):
        """Stop the bot"""
        await self.send_message("🛑 Stopping bot...")
        
        # Set running flag to False
        self.bot.running = False
        
        # Cancel scanner task if it exists
        if hasattr(self.bot, 'scanner_task'):
            self.bot.scanner_task.cancel()
            await self.send_message("📡 Scanner stopped")
        
        # Stop scanner if it has a stop method
        if hasattr(self.bot, 'scanner') and self.bot.scanner:
            if hasattr(self.bot.scanner, 'stop'):
                await self.bot.scanner.stop()
        
        # Close all positions if requested
        if args and args[0] == 'all':
            await self.send_message("📊 Closing all positions...")
            positions_to_close = list(self.bot.positions.keys()) if hasattr(self.bot, 'positions') else []
            for mint in positions_to_close:
                try:
                    await self.bot._close_position(mint, reason="manual_stop")
                except Exception as e:
                    logger.error(f"Failed to close position {mint}: {e}")
            await self.send_message(f"✅ Closed {len(positions_to_close)} positions")
        
        await self.send_message("✅ Bot stopped")
    
    async def cmd_pause(self, args):
        """Pause new trades"""
        self.bot.paused = True
        await self.send_message("⏸️ Bot paused - no new trades will be opened")
    
    async def cmd_resume(self, args):
        """Resume trading"""
        self.bot.paused = False
        await self.send_message("▶️ Bot resumed - trading enabled")
    
    async def cmd_status(self, args):
        """Get bot status"""
        try:
            status = "🟢 Running" if getattr(self.bot, 'running', False) else "🔴 Stopped"
            paused = "⏸️ Paused" if getattr(self.bot, 'paused', False) else "▶️ Active"
            
            sol_balance = 0
            if hasattr(self.bot, 'wallet'):
                sol_balance = self.bot.wallet.get_sol_balance()
            
            active_positions = len(getattr(self.bot, 'positions', {}))
            max_positions = getattr(self.bot, 'MAX_POSITIONS', 5)
            total_trades = getattr(self.bot, 'total_trades', 0)
            
            message = f"""
*🤖 BOT STATUS*
━━━━━━━━━━━━━━━━━━
Status: {status}
Trading: {paused}
SOL Balance: {sol_balance:.4f}
Active Positions: {active_positions}/{max_positions}
Total Trades: {total_trades}
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting status: {e}")
    
    async def cmd_wallet(self, args):
        """Get wallet info"""
        try:
            if not hasattr(self.bot, 'wallet'):
                await self.send_message("❌ Wallet not initialized")
                return
            
            sol_balance = self.bot.wallet.get_sol_balance()
            token_accounts = self.bot.wallet.get_all_token_accounts()
            
            tradeable_balance = max(0, sol_balance - 0.5)
            available_trades = int(tradeable_balance / 0.02)
            
            message = f"""
*💳 WALLET INFO*
━━━━━━━━━━━━━━━━━━
Address: `{str(self.bot.wallet.pubkey)[:20]}...`
SOL Balance: {sol_balance:.4f}
Token Positions: {len([t for t in token_accounts.values() if t['balance'] > 0])}
Available Trades: {available_trades}
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting wallet info: {e}")
    
    async def cmd_positions(self, args):
        """List active positions"""
        try:
            positions = getattr(self.bot, 'positions', {})
            
            if not positions:
                await self.send_message("📊 No active positions")
                return
            
            message = "*📈 ACTIVE POSITIONS*\n━━━━━━━━━━━━━━━━━━\n"
            
            for mint, pos in list(positions.items())[:10]:  # Limit to 10
                age = (datetime.now().timestamp() - pos.entry_time) / 60
                pnl_percent = getattr(pos, 'pnl_percent', 0)
                pnl_emoji = "🟢" if pnl_percent > 0 else "🔴"
                status = getattr(pos, 'status', 'unknown')
                
                message += f"""
Token: `{mint[:8]}...`
P&L: {pnl_emoji} {pnl_percent:+.1f}%
Age: {age:.1f} min
Status: {status}
━━━━━━━━━━━━━━━━━━
                """
            
            if len(positions) > 10:
                message += f"\n_...and {len(positions) - 10} more_"
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting positions: {e}")
    
    async def cmd_stats(self, args):
        """Get detailed statistics"""
        try:
            # Safe attribute access with defaults
            total_trades = getattr(self.bot, 'total_trades', 0)
            profitable_trades = getattr(self.bot, 'profitable_trades', 0)
            total_pnl = getattr(self.bot, 'total_pnl', 0)
            
            win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
            avg_pnl = (total_pnl / total_trades) if total_trades > 0 else 0
            
            # Safe scanner access
            launches_seen = 0
            launches_processed = 0
            
            # Try different possible scanner attribute names
            scanner = None
            if hasattr(self.bot, 'scanner'):
                scanner = self.bot.scanner
            elif hasattr(self.bot, 'monitor'):
                scanner = self.bot.monitor
            
            if scanner:
                launches_seen = getattr(scanner, 'launches_seen', 0)
                launches_processed = getattr(scanner, 'launches_processed', 0)
            
            message = f"""
*📊 STATISTICS*
━━━━━━━━━━━━━━━━━━
Total Trades: {total_trades}
Profitable: {profitable_trades}
Win Rate: {win_rate:.1f}%
Average P&L: {avg_pnl:+.1f}%
Total P&L: {total_pnl:+.1f}%

Launches Seen: {launches_seen}
Launches Traded: {launches_processed}
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting stats: {e}")
    
    async def cmd_pnl(self, args):
        """Get P&L summary"""
        try:
            # Calculate session P&L
            session_pnl_sol = 0
            positions = getattr(self.bot, 'positions', {})
            
            for pos in positions.values():
                pnl_percent = getattr(pos, 'pnl_percent', 0)
                amount_sol = getattr(pos, 'amount_sol', 0)
                if pnl_percent != 0:
                    session_pnl_sol += (amount_sol * pnl_percent / 100)
            
            sol_price = 250  # Approximate, could fetch from API
            session_pnl_usd = session_pnl_sol * sol_price
            
            total_trades = getattr(self.bot, 'total_trades', 0)
            profitable_trades = getattr(self.bot, 'profitable_trades', 0)
            win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
            
            message = f"""
*💰 P&L SUMMARY*
━━━━━━━━━━━━━━━━━━
Session P&L (SOL): {session_pnl_sol:+.4f}
Session P&L (USD): ${session_pnl_usd:+.2f}
Total Trades: {total_trades}
Win Rate: {win_rate:.1f}%
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting P&L: {e}")
    
    async def cmd_config(self, args):
        """Show current configuration"""
        try:
            from config import (
                BUY_AMOUNT_SOL, MAX_POSITIONS,
                STOP_LOSS_PERCENTAGE, TAKE_PROFIT_PERCENTAGE,
                MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL
            )
            
            message = f"""
*⚙️ CONFIGURATION*
━━━━━━━━━━━━━━━━━━
Buy Amount: {BUY_AMOUNT_SOL} SOL
Max Positions: {MAX_POSITIONS}
Stop Loss: -{STOP_LOSS_PERCENTAGE}%
Take Profit: +{TAKE_PROFIT_PERCENTAGE}%
Min Curve SOL: {MIN_BONDING_CURVE_SOL}
Max Curve SOL: {MAX_BONDING_CURVE_SOL}
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting config: {e}")
    
    async def cmd_help(self, args):
        """Show help message"""
        message = """
*📚 AVAILABLE COMMANDS*
━━━━━━━━━━━━━━━━━━
/start - Start the bot
/stop [all] - Stop bot (all=close positions)
/pause - Pause new trades
/resume - Resume trading
/status - Bot status
/wallet - Wallet info
/positions - Active positions
/stats - Trading statistics
/pnl - P&L summary
/config - Current settings
/force_sell <mint> - Force sell position
/blacklist <mint> - Blacklist token
/logs [n] - Recent logs (default 10)
/set_sl <percent> - Set stop loss
/set_tp <percent> - Set take profit
/help - This message
━━━━━━━━━━━━━━━━━━
        """
        
        await self.send_message(message)
    
    async def cmd_force_sell(self, args):
        """Force sell a position"""
        if not args:
            await self.send_message("❌ Usage: /force_sell <mint_address> or /force_sell all")
            return
        
        mint = args[0]
        positions = getattr(self.bot, 'positions', {})
        
        # Handle 'all' argument
        if mint.lower() == 'all':
            if not positions:
                await self.send_message("📊 No positions to close")
                return
            
            await self.send_message(f"📊 Closing {len(positions)} positions...")
            closed = 0
            failed = 0
            
            for mint_addr in list(positions.keys()):
                try:
                    await self.bot._close_position(mint_addr, reason="force_sell_all")
                    closed += 1
                except Exception as e:
                    logger.error(f"Failed to close {mint_addr}: {e}")
                    failed += 1
            
            msg = f"✅ Closed {closed} positions"
            if failed > 0:
                msg += f" (⚠️ {failed} failed)"
            await self.send_message(msg)
            return
        
        # Find position by partial match
        found_mint = None
        for pos_mint in positions.keys():
            if pos_mint.startswith(mint) or mint in pos_mint:
                found_mint = pos_mint
                break
        
        if found_mint:
            await self.send_message(f"📊 Force selling {found_mint[:8]}...")
            try:
                await self.bot._close_position(found_mint, reason="manual_force_sell")
                await self.send_message("✅ Position closed")
            except Exception as e:
                await self.send_message(f"❌ Failed to close position: {e}")
        else:
            await self.send_message(f"❌ Position not found: {mint}")
    
    async def cmd_blacklist(self, args):
        """Add token to blacklist"""
        if not args:
            await self.send_message("❌ Usage: /blacklist <mint_address>")
            return
        
        mint = args[0]
        try:
            from config import BLACKLISTED_TOKENS
            BLACKLISTED_TOKENS.add(mint)
            await self.send_message(f"✅ Added {mint[:8]}... to blacklist")
        except Exception as e:
            await self.send_message(f"❌ Failed to add to blacklist: {e}")
    
    async def cmd_recent_logs(self, args):
        """Get recent log entries"""
        # Since we're on Render, direct to dashboard
        await self.send_message("📝 Logs are available in your Render dashboard:\nhttps://dashboard.render.com\n\nCheck the service logs section.")
    
    async def cmd_set_stop_loss(self, args):
        """Set stop loss percentage"""
        if not args:
            await self.send_message("❌ Usage: /set_sl <percentage>")
            return
        
        try:
            new_sl = float(args[0])
            if 10 <= new_sl <= 90:
                import config
                config.STOP_LOSS_PERCENTAGE = new_sl
                await self.send_message(f"✅ Stop loss set to {new_sl}%")
            else:
                await self.send_message("❌ Stop loss must be between 10% and 90%")
        except ValueError:
            await self.send_message("❌ Invalid percentage. Use a number like: /set_sl 25")
        except Exception as e:
            await self.send_message(f"❌ Error setting stop loss: {e}")
    
    async def cmd_set_take_profit(self, args):
        """Set take profit percentage"""
        if not args:
            await self.send_message("❌ Usage: /set_tp <percentage>")
            return
        
        try:
            new_tp = float(args[0])
            if 50 <= new_tp <= 1000:
                import config
                config.TAKE_PROFIT_PERCENTAGE = new_tp
                await self.send_message(f"✅ Take profit set to {new_tp}%")
            else:
                await self.send_message("❌ Take profit must be between 50% and 1000%")
        except ValueError:
            await self.send_message("❌ Invalid percentage. Use a number like: /set_tp 200")
        except Exception as e:
            await self.send_message(f"❌ Error setting take profit: {e}")
    
    # ============================================
    # NOTIFICATION METHODS
    # ============================================
    
    async def notify_buy(self, mint: str, amount: float, signature: str):
        """Notify on buy execution"""
        message = f"""
*🟢 BUY EXECUTED*
Token: `{mint[:16]}...`
Amount: {amount} SOL
[View TX](https://solscan.io/tx/{signature})
        """
        await self.send_message(message)
    
    async def notify_sell(self, mint: str, pnl_percent: float, pnl_usd: float, reason: str):
        """Notify on sell execution"""
        emoji = "💰" if pnl_percent > 0 else "🔴"
        
        message = f"""
*{emoji} SELL EXECUTED*
Token: `{mint[:16]}...`
P&L: {pnl_percent:+.1f}% (${pnl_usd:+.2f})
Reason: {reason}
        """
        await self.send_message(message)
    
    async def notify_profit_milestone(self, total_pnl_sol: float, total_pnl_usd: float):
        """Notify on profit milestones"""
        message = f"""
*🎯 PROFIT MILESTONE*
Total P&L: {total_pnl_sol:+.4f} SOL
USD Value: ${total_pnl_usd:+.2f}
Keep it up! 🚀
        """
        await self.send_message(message)
    
    async def notify_error(self, error_type: str, details: str):
        """Notify on critical errors"""
        message = f"""
*⚠️ ERROR ALERT*
Type: {error_type}
Details: {details}
Check logs for more info.
        """
        await self.send_message(message)
    
    async def notify_launch_found(self, mint: str, metadata: dict):
        """Notify when a potential launch is found"""
        name = metadata.get('name', 'Unknown')
        symbol = metadata.get('symbol', 'N/A')
        
        message = f"""
*🔍 LAUNCH DETECTED*
Token: {name} ({symbol})
Mint: `{mint[:16]}...`
Analyzing...
        """
        await self.send_message(message)
    
    def stop(self):
        """Stop the Telegram bot"""
        self.running = False
        logger.info("Telegram bot stopped")
