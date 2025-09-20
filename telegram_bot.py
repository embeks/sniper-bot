"""
Telegram Bot Integration - Complete control interface with all fixes
FIXED: No duplicate messages, working /help, proper stop/start, rate limiting
"""

import asyncio
import logging
import time
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
        self.last_message_time = 0
        self.polling_task = None  # FIXED: Track polling task to prevent duplicates
        
        # Command handlers
        self.commands = {
            '/start': self.cmd_start,
            '/stop': self.cmd_stop,
            '/restart': self.cmd_restart,
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
            '/perf': self.cmd_perf,  # ADDED: Performance command
        }
        
        if ENABLE_TELEGRAM_NOTIFICATIONS:
            logger.info("✅ Telegram bot initialized")
    
    async def send_message(self, text: str, parse_mode: str = "Markdown"):
        """Send message to Telegram with rate limiting"""
        try:
            if not ENABLE_TELEGRAM_NOTIFICATIONS:
                return
            
            # Rate limiting
            current_time = time.time()
            time_since_last = current_time - self.last_message_time
            if time_since_last < 0.5:
                await asyncio.sleep(0.5 - time_since_last)
            
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    'chat_id': self.chat_id,
                    'text': text[:4096],
                    'parse_mode': parse_mode
                }
                
                async with session.post(url, json=payload) as resp:
                    self.last_message_time = time.time()
                    
                    if resp.status == 429:
                        retry_after = int(resp.headers.get('Retry-After', 5))
                        logger.warning(f"Rate limited, waiting {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                        # Retry once
                        async with session.post(url, json=payload) as retry_resp:
                            if retry_resp.status != 200:
                                error_text = await retry_resp.text()
                                logger.error(f"Failed after retry: {error_text}")
                    elif resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"Failed to send: {error_text}")
                        
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
    
    async def start_polling(self):
        """Poll for commands"""
        # FIXED: Prevent duplicate polling
        if self.polling_task and not self.polling_task.done():
            logger.warning("Polling already active, skipping duplicate start")
            return self.polling_task
        
        self.running = True
        logger.info("📱 Telegram polling started")
        logger.info(f"Bot token: {self.token[:10]}...")
        logger.info(f"Chat ID: {self.chat_id}")
        
        await self.send_message("📱 Telegram polling active - commands ready")
        
        while self.running:
            try:
                await self.get_updates()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Polling error: {e}")
                import traceback
                logger.error(traceback.format_exc())
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
                            
                            # Skip old messages (older than 60 seconds)
                            message = update.get('message', {})
                            if message.get('date'):
                                message_time = message['date']
                                current_time = time.time()
                                if current_time - message_time > 60:
                                    logger.debug(f"Skipping old message")
                                    continue
                            
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
            
            logger.info(f"📱 Telegram command received: {text}")
            
            # Parse command
            parts = text.split()
            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []
            
            # Execute command
            if command in self.commands:
                logger.info(f"Executing command: {command}")
                handler = self.commands[command]
                await handler(args)
            elif text.startswith('/'):
                await self.send_message("❌ Unknown command. Type /help for available commands.")
                
        except Exception as e:
            logger.error(f"Failed to process update: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await self.send_message(f"❌ Error processing command: {e}")
    
    # ============================================
    # COMMAND HANDLERS
    # ============================================
    
    async def cmd_start(self, args):
        """Start the bot"""
        if getattr(self.bot, 'running', False) and not getattr(self.bot, 'shutdown_requested', False):
            await self.send_message("✅ Bot is already running")
        else:
            await self.send_message("🚀 Starting bot...")
            await self.bot.start_scanner()
            await self.send_message("✅ Bot started successfully - monitoring for launches")
    
    async def cmd_stop(self, args):
        """Stop the bot"""
        await self.send_message("🛑 Stopping bot...")
        
        await self.bot.stop_scanner()
        
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
    
    async def cmd_restart(self, args):
        """Restart the bot"""
        await self.send_message("🔄 Restarting bot...")
        await self.bot.restart_bot()
        await self.send_message("✅ Bot restarted successfully")
    
    async def cmd_pause(self, args):
        """Pause new trades"""
        self.bot.paused = True
        await self.send_message("⏸️ Bot paused - no new trades will be opened")
    
    async def cmd_resume(self, args):
        """Resume trading"""
        self.bot.paused = False
        await self.send_message("▶️ Bot resumed - trading enabled")
    
    async def cmd_status(self, args):
        """Get bot status with scanner state"""
        try:
            scanner_status = await self.bot.get_scanner_status()
            
            # Determine overall status
            if scanner_status['shutdown_requested']:
                status = "🔴 Stopped"
            elif scanner_status['scanner_alive']:
                status = "🟢 Running"
            else:
                status = "🟡 Starting"
            
            paused = "⏸️ Paused" if scanner_status['paused'] else "▶️ Active"
            scanner = "🟢 Live" if scanner_status['scanner_alive'] else "🔴 Dead"
            
            sol_balance = self.bot.wallet.get_sol_balance()
            can_trade = "✅ Yes" if scanner_status['can_trade'] else "❌ No"
            
            message = f"""
*🤖 BOT STATUS*
━━━━━━━━━━━━━━━━━━
Status: {status}
Scanner: {scanner}
Trading: {paused}
Can Trade: {can_trade}
SOL Balance: {sol_balance:.4f}
Positions: {scanner_status['positions']}/{self.bot.MAX_POSITIONS}
Total Trades: {self.bot.total_trades}
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting status: {e}")
    
    async def cmd_wallet(self, args):
        """Get wallet info"""
        try:
            sol_balance = self.bot.wallet.get_sol_balance()
            token_accounts = self.bot.wallet.get_all_token_accounts()
            
            from config import MIN_SOL_BALANCE, BUY_AMOUNT_SOL
            tradeable_balance = max(0, sol_balance - MIN_SOL_BALANCE)
            available_trades = int(tradeable_balance / BUY_AMOUNT_SOL)
            
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
            
            for mint, pos in list(positions.items())[:10]:
                age = (time.time() - pos.entry_time) / 60
                pnl_emoji = "🟢" if pos.pnl_percent > 0 else "🔴"
                targets_hit = ', '.join(pos.partial_sells.keys()) if hasattr(pos, 'partial_sells') and pos.partial_sells else 'None'
                
                message += f"""
Token: `{mint[:8]}...`
P&L: {pnl_emoji} {pos.pnl_percent:+.1f}%
Targets Hit: {targets_hit}
Age: {age:.1f} min
Status: {pos.status}
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
            total_trades = getattr(self.bot, 'total_trades', 0)
            profitable_trades = getattr(self.bot, 'profitable_trades', 0)
            total_pnl = getattr(self.bot, 'total_pnl', 0)
            total_realized_sol = getattr(self.bot, 'total_realized_sol', 0)
            
            win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
            avg_pnl = (total_pnl / total_trades) if total_trades > 0 else 0
            
            launches_seen = 0
            launches_processed = 0
            scanner = getattr(self.bot, 'scanner', None)
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
Realized: {total_realized_sol:+.4f} SOL

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
            positions = getattr(self.bot, 'positions', {})
            
            # Calculate session P&L
            session_pnl_sol = getattr(self.bot, 'total_realized_sol', 0)
            
            # Add unrealized P&L
            for pos in positions.values():
                if hasattr(pos, 'pnl_percent') and pos.pnl_percent != 0:
                    unrealized = (pos.amount_sol * pos.pnl_percent / 100)
                    session_pnl_sol += unrealized
            
            sol_price = 250
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
                MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL,
                DRY_RUN
            )
            
            mode = "DRY RUN" if DRY_RUN else "LIVE"
            
            message = f"""
*⚙️ CONFIGURATION*
━━━━━━━━━━━━━━━━━━
Mode: {mode}
Buy Amount: {BUY_AMOUNT_SOL} SOL
Max Positions: {MAX_POSITIONS}
Stop Loss: -{STOP_LOSS_PERCENTAGE}%
Take Profit: +{TAKE_PROFIT_PERCENTAGE}%
Targets: 2x→40%, 3x→30%, 5x→30%
Min Curve: {MIN_BONDING_CURVE_SOL} SOL
Max Curve: {MAX_BONDING_CURVE_SOL} SOL
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting config: {e}")
    
    async def cmd_help(self, args):
        """Show help message - simplified for reliability"""
        message = (
            "📚 COMMANDS:\n\n"
            "/start - Start the bot\n"
            "/stop - Stop bot\n"
            "/stop all - Stop and close positions\n"
            "/restart - Restart bot\n"
            "/pause - Pause trading\n"
            "/resume - Resume trading\n"
            "/status - Bot status\n"
            "/wallet - Wallet info\n"
            "/positions - Active positions\n"
            "/stats - Statistics\n"
            "/pnl - P&L summary\n"
            "/config - Settings\n"
            "/perf - Performance metrics\n"
            "/force_sell all - Close all\n"
            "/force_sell <mint> - Close one\n"
            "/set_sl <pct> - Set stop loss\n"
            "/set_tp <pct> - Set take profit\n"
            "/help - This message"
        )
        
        await self.send_message(message)
    
    async def cmd_perf(self, args):
        """Get performance metrics from tracker"""
        try:
            if hasattr(self.bot, 'tracker'):
                stats = self.bot.tracker.get_session_stats()
                
                message = f"""
*📊 PERFORMANCE METRICS*
━━━━━━━━━━━━━━━━━━━━━
Session: {stats['session_duration_minutes']:.1f} min
Buys: {stats['total_buys']}
Sells: {stats['total_sells']}
Volume: {stats['total_volume_sol']:.4f} SOL
Fees Paid: {stats['total_fees_sol']:.6f} SOL
P&L: {stats['total_pnl_sol']:+.4f} SOL
Win Rate: {stats['win_rate_percent']:.1f}%
Avg Detection: {stats['avg_detection_time_ms']:.1f}ms
Avg Execution: {stats['avg_execution_time_ms']:.1f}ms
━━━━━━━━━━━━━━━━━━━━━
                """
                await self.send_message(message)
            else:
                await self.send_message("Performance tracker not initialized")
                
        except Exception as e:
            await self.send_message(f"❌ Error getting performance: {e}")
    
    async def cmd_force_sell(self, args):
        """Force sell a position"""
        if not args:
            await self.send_message("❌ Usage: /force_sell <mint> or /force_sell all")
            return
        
        mint = args[0]
        positions = getattr(self.bot, 'positions', {})
        
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
                await self.send_message(f"❌ Failed: {e}")
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
            await self.send_message(f"❌ Failed: {e}")
    
    async def cmd_recent_logs(self, args):
        """Get recent log entries"""
        await self.send_message(
            "📝 Logs are available in your Render dashboard:\n"
            "https://dashboard.render.com\n\n"
            "Check the service logs section."
        )
    
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
            await self.send_message("❌ Invalid percentage")
        except Exception as e:
            await self.send_message(f"❌ Error: {e}")
    
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
            await self.send_message("❌ Invalid percentage")
        except Exception as e:
            await self.send_message(f"❌ Error: {e}")
    
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
        """
        await self.send_message(message)
    
    def stop(self):
        """Stop the Telegram bot"""
        self.running = False
        logger.info("Telegram bot stopped")
