"""
Trade Logger - Clean CSV with ONE ROW per completed trade
"""

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


class TradeLogger:
    def __init__(self, csv_path: str = "/data/trades_clean.csv"):
        self.csv_path = Path(csv_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.csv_path.exists():
            self._write_headers()
            logger.info(f"Created trade log: {self.csv_path}")

    def _write_headers(self):
        headers = [
            'date', 'token', 'entry_curve', 'peak_curve', 'exit_curve',
            'entry_buyers', 'exit_reason', 'hold_secs', 'sells_survived',
            'invested', 'received', 'pnl_sol', 'pnl_pct', 'result', 'max_pnl_pct'
        ]
        with open(self.csv_path, 'w', newline='') as f:
            csv.writer(f).writerow(headers)

    def log_trade(
        self, mint: str, entry_curve: float, peak_curve: float, exit_curve: float,
        entry_buyers: int, exit_reason: str, hold_secs: float, sells_survived: int,
        invested: float, received: float, max_pnl_pct: float
    ):
        pnl_sol = received - invested
        pnl_pct = ((received / invested) - 1) * 100 if invested > 0 else 0

        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            mint[:8], f"{entry_curve:.2f}", f"{peak_curve:.2f}", f"{exit_curve:.2f}",
            entry_buyers, exit_reason, f"{hold_secs:.1f}", sells_survived,
            f"{invested:.4f}", f"{received:.4f}", f"{pnl_sol:+.4f}",
            f"{pnl_pct:+.1f}%", "WIN" if pnl_sol > 0 else "LOSS", f"{max_pnl_pct:+.1f}%"
        ]

        with open(self.csv_path, 'a', newline='') as f:
            csv.writer(f).writerow(row)

        logger.info(f"Logged: {mint[:8]} | {'WIN' if pnl_sol > 0 else 'LOSS'} | {pnl_sol:+.4f} SOL")
