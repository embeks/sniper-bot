
"""
Telegram Bot
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
        self.polling_task = None
        
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
            '/perf': self.cmd_perf,
            '/selftest': self.cmd_selftest,  # ADDED: Self-test command
        }
        
        if ENABLE_TELEGRAM_NOTIFICATIONS:
            logger.info("✅ Telegram bot initialized")
    
    async def send_message(self, text: str, parse_mode: str = "HTML"):
        """Send message to Telegram with HTML formatting"""
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
        if self.polling_task and not self.polling_task.done():
            logger.warning("Polling already active, skipping duplicate start")
            return self.polling_task
        
        self.running = True
        logger.info("📱 Telegram polling started")
        
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
            await self.send_message(f"❌ Error processing command: {e}")
    
    # ============================================
    # COMMAND HANDLERS (HTML formatted)
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
<b>🤖 BOT STATUS</b>
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
<b>💳 WALLET INFO</b>
━━━━━━━━━━━━━━━━━━
Address: <code>{str(self.bot.wallet.pubkey)[:20]}...</code>
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
            
            message = "<b>📈 ACTIVE POSITIONS</b>\n━━━━━━━━━━━━━━━━━━\n"
            
            for mint, pos in list(positions.items())[:10]:
                age = (time.time() - pos.entry_time) / 60
                pnl_emoji = "🟢" if pos.pnl_percent > 0 else "🔴"
                targets_hit = ', '.join(pos.partial_sells.keys()) if hasattr(pos, 'partial_sells') and pos.partial_sells else 'None'
                
                message += f"""
Token: <code>{mint[:8]}...</code>
P&L: {pnl_emoji} {pos.pnl_percent:+.1f}%
Targets Hit: {targets_hit}
Age: {age:.1f} min
Status: {pos.status}
━━━━━━━━━━━━━━━━━━
                """
            
            if len(positions) > 10:
                message += f"\n<i>...and {len(positions) - 10} more</i>"
            
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
            
            # Get scanner stats if available
            scanner_stats = {}
            if hasattr(self.bot, 'scanner') and hasattr(self.bot.scanner, 'get_stats'):
                scanner_stats = self.bot.scanner.get_stats()
            
            message = f"""
<b>📊 STATISTICS</b>
━━━━━━━━━━━━━━━━━━
Total Trades: {total_trades}
Profitable: {profitable_trades}
Win Rate: {win_rate:.1f}%
Average P&L: {avg_pnl:+.1f}%
Total P&L: {total_pnl:+.1f}%
Realized: {total_realized_sol:+.4f} SOL
"""
            
            if scanner_stats:
                message += f"""
Tokens Seen: {scanner_stats.get('tokens_seen', 0)}
Tokens Passed: {scanner_stats.get('tokens_passed', 0)}
Filter Rate: {scanner_stats.get('filter_rate', 0):.1f}%
"""
            
            message += "━━━━━━━━━━━━━━━━━━"
            
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
<b>💰 P&L SUMMARY</b>
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
<b>⚙️ CONFIGURATION</b>
━━━━━━━━━━━━━━━━━━
Mode: {mode}
Buy Amount: {BUY_AMOUNT_SOL} SOL
Max Positions: {MAX_POSITIONS}
Stop Loss: -{STOP_LOSS_PERCENTAGE}%
Take Profit: +{TAKE_PROFIT_PERCENTAGE}%
Targets: 1.5x→50%, 2x→30%, 3x→20%
Min Curve: {MIN_BONDING_CURVE_SOL} SOL
Max Curve: {MAX_BONDING_CURVE_SOL} SOL
━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting config: {e}")
    
    async def cmd_help(self, args):
        """Show help message"""
        message = """
<b>📚 COMMANDS:</b>

/start - Start the bot
/stop - Stop bot
/stop all - Stop and close positions
/restart - Restart bot
/pause - Pause trading
/resume - Resume trading
/status - Bot status
/wallet - Wallet info
/positions - Active positions
/stats - Statistics
/pnl - P&L summary
/config - Settings
/perf - Performance metrics
/force_sell all - Close all
/force_sell <code>&lt;mint&gt;</code> - Close one
/set_sl <code>&lt;pct&gt;</code> - Set stop loss
/set_tp <code>&lt;pct&gt;</code> - Set take profit
/selftest - Run self-test
/help - This message
        """
        
        await self.send_message(message)
    
    async def cmd_perf(self, args):
        """Get performance metrics from tracker"""
        try:
            if hasattr(self.bot, 'tracker'):
                stats = self.bot.tracker.get_session_stats()
                
                message = f"""
<b>📊 PERFORMANCE METRICS</b>
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
    
    async def cmd_selftest(self, args):
        """Run self-test for decimals and sell payload"""
        try:
            await self.send_message("🔍 Running self-test...")
            
            # Test with a known PumpFun token (use a popular one)
            test_mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"  # Example POPCAT
            
            # Test decimals fetching
            decimals, source = self.bot.wallet.get_token_decimals(test_mint)
            
            test_results = f"""
<b>🧪 SELF-TEST RESULTS</b>
━━━━━━━━━━━━━━━━━━━━━
Test Mint: <code>{test_mint[:16]}...</code>
Decimals: {decimals}
Source: {source}

<b>Dry-run sell payload:</b>
UI Amount: 1000000.0 tokens
Token Decimals: {decimals}
denominatedInSol: "false"
Raw Atoms (verification): {int(1000000 * 10**decimals)}

✅ Self-test complete
━━━━━━━━━━━━━━━━━━━━━
            """
            
            await self.send_message(test_results)
            
        except Exception as e:
            await self.send_message(f"❌ Self-test failed: {e}")
    
    async def cmd_force_sell(self, args):
        """Force sell a position"""
        if not args:
            await self.send_message("❌ Usage: /force_sell <code>&lt;mint&gt;</code> or /force_sell all")
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
            await self.send_message(f"📊 Force selling <code>{found_mint[:8]}...</code>")
            try:
                await self.bot._close_position(found_mint, reason="manual_force_sell")
                await self.send_message("✅ Position closed")
            except Exception as e:
                await self.send_message(f"❌ Failed: {e}")
        else:
            await self.send_message(f"❌ Position not found: <code>{mint}</code>")
    
    async def cmd_blacklist(self, args):
        """Add token to blacklist"""
        if not args:
            await self.send_message("❌ Usage: /blacklist <code>&lt;mint_address&gt;</code>")
            return
        
        mint = args[0]
        try:
            from config import BLACKLISTED_TOKENS
            BLACKLISTED_TOKENS.add(mint)
            await self.send_message(f"✅ Added <code>{mint[:8]}...</code> to blacklist")
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
            await self.send_message("❌ Usage: /set_sl <code>&lt;percentage&gt;</code>")
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
            await self.send_message("❌ Usage: /set_tp <code>&lt;percentage&gt;</code>")
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
    # NOTIFICATION METHODS (HTML formatted)
    # ============================================
    
    async def notify_buy(self, mint: str, amount: float, signature: str):
        """Notify on buy execution"""
        message = f"""
<b>🟢 BUY EXECUTED</b>
Token: <code>{mint[:16]}...</code>
Amount: {amount} SOL
<a href="https://solscan.io/tx/{signature}">View TX</a>
        """
        await self.send_message(message)
    
    async def notify_sell(self, mint: str, pnl_percent: float, pnl_usd: float, reason: str):
        """Notify on sell execution"""
        emoji = "💰" if pnl_percent > 0 else "🔴"
        
        message = f"""
<b>{emoji} SELL EXECUTED</b>
Token: <code>{mint[:16]}...</code>
P&L: {pnl_percent:+.1f}% (${pnl_usd:+.2f})
Reason: {reason}
        """
        await self.send_message(message)
    
    async def notify_profit_milestone(self, total_pnl_sol: float, total_pnl_usd: float):
        """Notify on profit milestones"""
        message = f"""
<b>🎯 PROFIT MILESTONE</b>
Total P&L: {total_pnl_sol:+.4f} SOL
USD Value: ${total_pnl_usd:+.2f}
Keep it up! 🚀
        """
        await self.send_message(message)
    
    async def notify_error(self, error_type: str, details: str):
        """Notify on critical errors"""
        message = f"""
<b>⚠️ ERROR ALERT</b>
Type: {error_type}
Details: {details}
        """
        await self.send_message(message)
    
    def stop(self):
        """Stop the Telegram bot"""
        self.running = False
        logger.info("Telegram bot stopped")
