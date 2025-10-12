"""
Performance Tracker - FIXED: Accurate win/loss tracking
"""

import json
import time
import logging
import os
import csv
from datetime import datetime
from typing import Dict, Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)

class PerformanceTracker:
    """Track and log all performance metrics to CSV on persistent disk"""
    
    def __init__(self, events_file: str = "events.jsonl"):
        """Initialize performance tracker"""
        self.events_file = Path(events_file)
        self.session_start = time.time()
        
        # Use Render persistent disk path
        self.csv_file = Path("/data/trades.csv")
        
        # Ensure /data directory exists (Render should create it, but be safe)
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize CSV file
        self.setup_csv()
        
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
        
        # High-frequency events to skip
        self.high_frequency_events = ['position_update', 'token_detected']
        
        logger.info(f"📊 Performance tracker initialized (ACCURATE STATS)")
        logger.info(f"📈 Logging trades to {self.csv_file}")
    
    def setup_csv(self):
        """Initialize CSV file with headers if it doesn't exist"""
        file_exists = self.csv_file.exists()
        
        if not file_exists:
            # Create new file with headers
            with open(self.csv_file, 'w', newline='') as f:
                fieldnames = [
                    'timestamp', 'event_type', 'mint', 'amount_sol', 
                    'pnl_sol', 'fees_sol', 'tokens', 'execution_ms', 'reason'
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
            logger.info(f"✅ Created new CSV file: {self.csv_file}")
        else:
            logger.info(f"📝 Appending to existing CSV: {self.csv_file}")
        
        # Log startup event
        self.append_to_csv({
            'timestamp': datetime.now().isoformat(),
            'event_type': 'bot_started',
            'mint': 'SYSTEM',
            'amount_sol': 0,
            'pnl_sol': 0,
            'fees_sol': 0,
            'tokens': 0,
            'execution_ms': 0,
            'reason': 'Performance tracking initialized (ACCURATE)'
        })
    
    def append_to_csv(self, data: Dict) -> bool:
        """Append a row to the CSV file"""
        try:
            with open(self.csv_file, 'a', newline='') as f:
                fieldnames = [
                    'timestamp', 'event_type', 'mint', 'amount_sol', 
                    'pnl_sol', 'fees_sol', 'tokens', 'execution_ms', 'reason'
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                
                # Prepare row data
                row = {
                    'timestamp': data.get('timestamp', datetime.now().isoformat()),
                    'event_type': data.get('event_type', ''),
                    'mint': data.get('mint', '')[:16] if data.get('mint') else '',
                    'amount_sol': data.get('amount_sol', 0),
                    'pnl_sol': data.get('pnl_sol', 0),
                    'fees_sol': data.get('fees_sol', 0),
                    'tokens': data.get('tokens', 0),
                    'execution_ms': data.get('execution_ms', 0),
                    'reason': data.get('reason', '')
                }
                
                writer.writerow(row)
                return True
                
        except Exception as e:
            logger.error(f"CSV write error: {e}")
            return False
    
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
        """Log an event to both JSONL and CSV"""
        try:
            # Create event record
            event = {
                'timestamp': datetime.now().isoformat(),
                'unix_time': time.time(),
                'event_type': event_type,
                'session_time': time.time() - self.session_start,
                **data
            }
            
            # Always write to local JSONL file for detailed logging
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
            
            # Skip high-frequency events for CSV
            if event_type in self.high_frequency_events:
                return
            
            # Prepare CSV data
            csv_data = {
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
            
            # Append to CSV
            self.append_to_csv(csv_data)
            
            # Update metrics
            if event_type == 'buy_executed':
                self.metrics['total_buys'] += 1
                self.metrics['positions_opened'] += 1
                self.metrics['total_volume_sol'] += data.get('total_cost', 0)
                self.metrics['total_fees_sol'] += data.get('total_fees', 0)
                
            elif event_type == 'sell_executed':
                self.metrics['total_sells'] += 1
                self.metrics['positions_closed'] += 1
                
                # FIXED: Only count as win if ACTUAL profit (not just positive pnl_sol from bug)
                pnl = data.get('pnl_sol', 0)
                
                # A real win means you made profit (pnl > 0.001 SOL minimum)
                # This filters out tiny positive values from rounding errors
                if pnl > 0.001:
                    self.metrics['winning_trades'] += 1
                    logger.debug(f"✅ Trade counted as WIN: {pnl:+.4f} SOL")
                else:
                    self.metrics['losing_trades'] += 1
                    logger.debug(f"❌ Trade counted as LOSS: {pnl:+.4f} SOL")
                
                self.metrics['total_pnl_sol'] += pnl
            
            # FIXED: Also track partial sells correctly
            elif event_type == 'partial_sell':
                # Partial sells with profit should be tracked
                pnl = data.get('pnl_sol', 0)
                self.metrics['total_pnl_sol'] += pnl
                logger.debug(f"📊 Partial sell P&L: {pnl:+.4f} SOL")
                
        except Exception as e:
            logger.error(f"Failed to log event: {e}")
    
    def log_token_detection(self, mint: str, source: str, detection_time_ms: float):
        """Log token detection event - high frequency, skip CSV"""
        self.metrics['detection_times'].append(detection_time_ms)
        
        # Only log to JSONL file
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
        
        logger.info(f"📊 Buy logged to CSV for {mint[:8]}...")
    
    def log_buy_failed(self, mint: str, amount_sol: float, error: str):
        """Log failed buy"""
        self.log_event('buy_failed', {
            'mint': mint,
            'amount_sol': amount_sol,
            'error': str(error)[:100]
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
        
        logger.info(f"📊 Sell logged to CSV for {mint[:8]}... P&L: {pnl_sol:+.4f} SOL")
    
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
        
        logger.info(f"📊 Partial sell logged to CSV for {mint[:8]}... Target: {target_name}")
    
    def log_position_update(self, mint: str, current_pnl_percent: float, 
                           current_price: float, age_seconds: float):
        """Log position monitoring update - high frequency, skip CSV"""
        # Only log to JSONL file
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
        """Get current session statistics - FIXED"""
        session_duration = time.time() - self.session_start
        
        # FIXED: Calculate win rate correctly
        win_rate = 0
        if self.metrics['positions_closed'] > 0:
            win_rate = (self.metrics['winning_trades'] / self.metrics['positions_closed']) * 100
        
        avg_detection_time = 0
        if self.metrics['detection_times']:
            avg_detection_time = sum(self.metrics['detection_times']) / len(self.metrics['detection_times'])
        
        avg_execution_time = 0
        if self.metrics['execution_times']:
            avg_execution_time = sum(self.metrics['execution_times']) / len(self.metrics['execution_times'])
        
        # FIXED: Calculate average P&L correctly
        avg_pnl_percent = 0
        if self.metrics['positions_closed'] > 0:
            # Average P&L per trade
            avg_pnl_sol = self.metrics['total_pnl_sol'] / self.metrics['positions_closed']
            # Convert to percentage (assuming 0.05 SOL entries)
            avg_pnl_percent = (avg_pnl_sol / 0.05) * 100
        
        return {
            'session_duration_minutes': session_duration / 60,
            'total_buys': self.metrics['total_buys'],
            'total_sells': self.metrics['total_sells'],
            'open_positions': self.metrics['positions_opened'] - self.metrics['positions_closed'],
            'total_volume_sol': self.metrics['total_volume_sol'],
            'total_fees_sol': self.metrics['total_fees_sol'],
            'total_pnl_sol': self.metrics['total_pnl_sol'],
            'win_rate_percent': win_rate,
            'avg_pnl_percent': avg_pnl_percent,
            'winning_trades': self.metrics['winning_trades'],
            'losing_trades': self.metrics['losing_trades'],
            'avg_detection_time_ms': avg_detection_time,
            'avg_execution_time_ms': avg_execution_time
        }
    
    def log_session_summary(self):
        """Log session summary"""
        stats = self.get_session_stats()
        
        # Log summary event
        self.log_event('session_summary', stats)
        
        # Also append summary as special row
        summary_data = {
            'timestamp': datetime.now().isoformat(),
            'event_type': 'SESSION_SUMMARY',
            'mint': 'SUMMARY',
            'amount_sol': stats['total_volume_sol'],
            'pnl_sol': stats['total_pnl_sol'],
            'fees_sol': stats['total_fees_sol'],
            'tokens': stats['total_buys'],
            'execution_ms': stats['avg_execution_time_ms'],
            'reason': f"Win rate: {stats['win_rate_percent']:.1f}% | W:{stats['winning_trades']} L:{stats['losing_trades']}"
        }
        
        self.append_to_csv(summary_data)
        
        logger.info("📊 SESSION PERFORMANCE SUMMARY")
        logger.info(f"Duration: {stats['session_duration_minutes']:.1f} minutes")
        logger.info(f"Trades: {stats['total_buys']} buys, {stats['total_sells']} sells")
        logger.info(f"Wins: {stats['winning_trades']} | Losses: {stats['losing_trades']}")
        logger.info(f"Win Rate: {stats['win_rate_percent']:.1f}%")
        logger.info(f"Volume: {stats['total_volume_sol']:.4f} SOL")
        logger.info(f"Fees Paid: {stats['total_fees_sol']:.6f} SOL")
        logger.info(f"P&L: {stats['total_pnl_sol']:+.4f} SOL")
        logger.info(f"Avg P&L: {stats['avg_pnl_percent']:+.1f}%")
        logger.info(f"Avg Detection: {stats['avg_detection_time_ms']:.1f}ms")
        logger.info(f"Avg Execution: {stats['avg_execution_time_ms']:.1f}ms")
        logger.info(f"📈 Full trade log saved to: {self.csv_file}")
        
        return stats

