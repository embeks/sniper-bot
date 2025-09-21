"""
Performance Tracker - Track bot performance metrics and fees
Logs all trading events to Google Sheets for easy access
FIXED: Skips high-frequency events to avoid Google Sheets rate limits
"""

import json
import time
import logging
import os
import requests
from datetime import datetime
from typing import Dict, Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)

class PerformanceTracker:
    """Track and log all performance metrics to Google Sheets"""
    
    def __init__(self, events_file: str = "events.jsonl"):
        """Initialize performance tracker"""
        self.events_file = Path(events_file)
        self.session_start = time.time()
        
        # Google Sheets setup
        self.setup_google_sheets()
        
        # Session metrics
        self.metrics = {
            'total_buys': 0,
            'total_sells': 0,
            'total_volume_sol': 0.0,
            'total_fees_sol': 0.0,
            'total_pnl_sol': 0.0,
            'detection_times': [],
            'execution_times': [],
            'positions_opened': 0,
            'positions_closed': 0,
            'winning_trades': 0,
            'losing_trades': 0,
        }
        
        # Fee breakdown
        self.fees = {
            'network_fee': 0.000005,  # Base network fee
            'priority_fee': 0.0001,   # Priority fee
            'platform_fee_rate': 0.01  # PumpFun 1% fee
        }
        
        # Track last sheet write time to avoid spam
        self.last_sheet_write = 0
        self.sheet_write_cooldown = 2  # Minimum seconds between sheet writes
        
        # High-frequency events to skip in sheets
        self.high_frequency_events = ['position_update', 'token_detected']
        
        logger.info(f"📊 Performance tracker initialized")
        if self.sheet_url:
            logger.info(f"📈 Sheety connected for Google Sheets logging")
    
    def setup_google_sheets(self):
        """Setup Google Sheets connection via Sheety"""
        # Only use SHEETY_URL - no need for GOOGLE_SHEET_ID
        self.sheet_url = os.getenv('SHEETY_URL', '')
        
        if not self.sheet_url:
            logger.warning("⚠️ No SHEETY_URL set - add to Render environment variables")
            logger.warning("Get your Sheety API URL from https://sheety.co")
        else:
            logger.info(f"✅ Sheety configured for Google Sheets logging")
            # Test connection
            self.test_sheet_connection()
    
    def test_sheet_connection(self):
        """Test if we can write to the sheet"""
        try:
            # Try to write a test row
            self.write_to_sheet({
                'timestamp': datetime.now().isoformat(),
                'event_type': 'bot_started',
                'mint': 'SYSTEM',
                'amount_sol': 0,
                'pnl_sol': 0,
                'fees_sol': 0,
                'tokens': 0,
                'execution_ms': 0,
                'reason': 'Performance tracking initialized'
            })
            logger.info("✅ Google Sheets connection successful")
        except Exception as e:
            logger.error(f"❌ Google Sheets connection failed: {e}")
            logger.error("Make sure your sheet is set to 'Anyone with link can edit'")
    
    def write_to_sheet(self, data: Dict):
        """Write a row to Google Sheets using Sheety with rate limiting"""
        try:
            if not self.sheet_url:
                return
            
            # FIXED: Rate limiting to avoid API limits
            current_time = time.time()
            time_since_last = current_time - self.last_sheet_write
            if time_since_last < self.sheet_write_cooldown:
                logger.debug(f"Skipping sheet write (cooldown: {self.sheet_write_cooldown - time_since_last:.1f}s)")
                return
            
            # Format data for Sheety API
            row = {
                'timestamp': data.get('timestamp', ''),
                'Event_Type': data.get('event_type', ''),
                'Mint': data.get('mint', '')[:8] if data.get('mint') else '',
                'Amount_Sol': float(data.get('amount_sol', 0)),
                'PnL_Sol': float(data.get('pnl_sol', 0)),
                'Fees_Sol': float(data.get('fees_sol', 0)),
                'Tokens': float(data.get('tokens', 0)),
                'Execution_Ms': float(data.get('execution_ms', 0)),
                'Reason': data.get('reason', '')
            }
            
            # Send to Sheety
            response = requests.post(self.sheet_url, json={'sheet1': row})
            if response.status_code == 200:
                self.last_sheet_write = current_time
            else:
                logger.debug(f"Sheet write failed: {response.status_code}")
                
        except Exception as e:
            logger.debug(f"Sheet write error: {e}")
            # Don't break the bot over logging issues
    
    def calculate_total_cost(self, buy_amount_sol: float) -> Dict:
        """Calculate total cost including all fees"""
        platform_fee = buy_amount_sol * self.fees['platform_fee_rate']
        total_fees = self.fees['network_fee'] + self.fees['priority_fee'] + platform_fee
        total_cost = buy_amount_sol + total_fees
        
        return {
            'buy_amount': buy_amount_sol,
            'network_fee': self.fees['network_fee'],
            'priority_fee': self.fees['priority_fee'],
            'platform_fee': platform_fee,
            'total_fees': total_fees,
            'total_cost': total_cost
        }
    
    def log_event(self, event_type: str, data: Dict):
        """Log an event to file and Google Sheets"""
        try:
            event = {
                'timestamp': datetime.now().isoformat(),
                'unix_time': time.time(),
                'event_type': event_type,
                'session_time': time.time() - self.session_start,
                **data
            }
            
            # Write to local file
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
            
            # FIXED: Skip high-frequency events for Google Sheets
            if event_type in self.high_frequency_events:
                logger.debug(f"Skipping sheet write for high-frequency event: {event_type}")
                return
            
            # Write to Google Sheets (with rate limiting)
            sheet_data = {
                'timestamp': datetime.now().isoformat(),
                'event_type': event_type,
                'mint': data.get('mint', ''),
                'amount_sol': data.get('amount_sol', data.get('buy_amount', 0)),
                'pnl_sol': data.get('pnl_sol', 0),
                'fees_sol': data.get('total_fees', 0),
                'tokens': data.get('tokens_received', data.get('tokens_sold', 0)),
                'execution_ms': data.get('execution_time_ms', 0),
                'reason': data.get('reason', '')
            }
            self.write_to_sheet(sheet_data)
            
            # Update metrics based on event type
            if event_type == 'buy_executed':
                self.metrics['total_buys'] += 1
                self.metrics['positions_opened'] += 1
                self.metrics['total_volume_sol'] += data.get('total_cost', 0)
                self.metrics['total_fees_sol'] += data.get('total_fees', 0)
                
            elif event_type == 'sell_executed':
                self.metrics['total_sells'] += 1
                self.metrics['positions_closed'] += 1
                if data.get('pnl_sol', 0) > 0:
                    self.metrics['winning_trades'] += 1
                else:
                    self.metrics['losing_trades'] += 1
                self.metrics['total_pnl_sol'] += data.get('pnl_sol', 0)
                
        except Exception as e:
            logger.error(f"Failed to log event: {e}")
    
    def log_token_detection(self, mint: str, source: str, detection_time_ms: float):
        """Log token detection event - high frequency, skip sheets"""
        self.metrics['detection_times'].append(detection_time_ms)
        
        # Only log to file, not sheets (high frequency)
        try:
            event = {
                'timestamp': datetime.now().isoformat(),
                'unix_time': time.time(),
                'event_type': 'token_detected',
                'mint': mint,
                'source': source,
                'detection_time_ms': detection_time_ms
            }
            
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
                
        except Exception as e:
            logger.debug(f"Failed to log token detection: {e}")
    
    def log_buy_attempt(self, mint: str, amount_sol: float, slippage: int):
        """Log buy attempt"""
        cost_breakdown = self.calculate_total_cost(amount_sol)
        
        self.log_event('buy_attempt', {
            'mint': mint,
            'amount_sol': amount_sol,
            'slippage': slippage,
            **cost_breakdown
        })
        
        return cost_breakdown
    
    def log_buy_executed(self, mint: str, amount_sol: float, signature: str, 
                        tokens_received: float, execution_time_ms: float):
        """Log successful buy execution"""
        cost_breakdown = self.calculate_total_cost(amount_sol)
        self.metrics['execution_times'].append(execution_time_ms)
        
        self.log_event('buy_executed', {
            'mint': mint,
            'signature': signature,
            'tokens_received': tokens_received,
            'execution_time_ms': execution_time_ms,
            **cost_breakdown
        })
    
    def log_buy_failed(self, mint: str, amount_sol: float, error: str):
        """Log failed buy"""
        self.log_event('buy_failed', {
            'mint': mint,
            'amount_sol': amount_sol,
            'error': str(error)
        })
    
    def log_sell_executed(self, mint: str, tokens_sold: float, signature: str,
                         sol_received: float, pnl_sol: float, pnl_percent: float,
                         hold_time_seconds: float, reason: str):
        """Log successful sell execution"""
        self.log_event('sell_executed', {
            'mint': mint,
            'signature': signature,
            'tokens_sold': tokens_sold,
            'sol_received': sol_received,
            'pnl_sol': pnl_sol,
            'pnl_percent': pnl_percent,
            'hold_time_seconds': hold_time_seconds,
            'reason': reason
        })
    
    def log_partial_sell(self, mint: str, target_name: str, percent_sold: float,
                        tokens_sold: float, sol_received: float, pnl_sol: float):
        """Log partial sell at profit target"""
        self.log_event('partial_sell', {
            'mint': mint,
            'target_name': target_name,
            'percent_sold': percent_sold,
            'tokens_sold': tokens_sold,
            'sol_received': sol_received,
            'pnl_sol': pnl_sol,
            'reason': target_name
        })
    
    def log_position_update(self, mint: str, current_pnl_percent: float, 
                           current_price: float, age_seconds: float):
        """Log position monitoring update - high frequency, skip sheets"""
        # Only log to file, not sheets (too many updates)
        try:
            event = {
                'timestamp': datetime.now().isoformat(),
                'unix_time': time.time(),
                'event_type': 'position_update',
                'mint': mint,
                'current_pnl_percent': current_pnl_percent,
                'current_price': current_price,
                'age_seconds': age_seconds
            }
            
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
                
        except Exception as e:
            logger.debug(f"Failed to log position update: {e}")
    
    def get_session_stats(self) -> Dict:
        """Get current session statistics"""
        session_duration = time.time() - self.session_start
        
        win_rate = 0
        if self.metrics['positions_closed'] > 0:
            win_rate = (self.metrics['winning_trades'] / self.metrics['positions_closed']) * 100
        
        avg_detection_time = 0
        if self.metrics['detection_times']:
            avg_detection_time = sum(self.metrics['detection_times']) / len(self.metrics['detection_times'])
        
        avg_execution_time = 0
        if self.metrics['execution_times']:
            avg_execution_time = sum(self.metrics['execution_times']) / len(self.metrics['execution_times'])
        
        return {
            'session_duration_minutes': session_duration / 60,
            'total_buys': self.metrics['total_buys'],
            'total_sells': self.metrics['total_sells'],
            'open_positions': self.metrics['positions_opened'] - self.metrics['positions_closed'],
            'total_volume_sol': self.metrics['total_volume_sol'],
            'total_fees_sol': self.metrics['total_fees_sol'],
            'total_pnl_sol': self.metrics['total_pnl_sol'],
            'win_rate_percent': win_rate,
            'avg_detection_time_ms': avg_detection_time,
            'avg_execution_time_ms': avg_execution_time
        }
    
    def log_session_summary(self):
        """Log session summary"""
        stats = self.get_session_stats()
        
        # Force write session summary to sheets (important event)
        self.last_sheet_write = 0  # Reset cooldown for important event
        self.log_event('session_summary', stats)
        
        # Also write summary to sheets with special formatting
        self.write_to_sheet({
            'timestamp': datetime.now().isoformat(),
            'event_type': 'SESSION_SUMMARY',
            'mint': 'SUMMARY',
            'amount_sol': stats['total_volume_sol'],
            'pnl_sol': stats['total_pnl_sol'],
            'fees_sol': stats['total_fees_sol'],
            'tokens': stats['total_buys'],
            'execution_ms': stats['avg_execution_time_ms'],
            'reason': f"Win rate: {stats['win_rate_percent']:.1f}%"
        })
        
        logger.info("📊 SESSION PERFORMANCE SUMMARY")
        logger.info(f"Duration: {stats['session_duration_minutes']:.1f} minutes")
        logger.info(f"Trades: {stats['total_buys']} buys, {stats['total_sells']} sells")
        logger.info(f"Volume: {stats['total_volume_sol']:.4f} SOL")
        logger.info(f"Fees Paid: {stats['total_fees_sol']:.6f} SOL")
        logger.info(f"P&L: {stats['total_pnl_sol']:+.4f} SOL")
        logger.info(f"Win Rate: {stats['win_rate_percent']:.1f}%")
        logger.info(f"Avg Detection: {stats['avg_detection_time_ms']:.1f}ms")
        logger.info(f"Avg Execution: {stats['avg_execution_time_ms']:.1f}ms")
        
        return stats
