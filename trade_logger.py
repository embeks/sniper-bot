"""
Trade Logger - Clean CSV with ONE ROW per completed trade
"""

import csv
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
            'date', 'token',
            'entry_curve', 'peak_curve', 'exit_curve_decision', 'exit_curve_final',
            'entry_buyers', 'entry_velocity', 'buyer_velocity', 'token_age_sec',
            'sells_at_entry', 'largest_buy_pct', 'top2_concentration', 'bundled',
            'exit_reason', 'hold_secs', 'peak_time_sec', 'peak_to_exit_sec', 'sells_survived',
            'invested', 'received', 'pnl_sol', 'pnl_pct', 'result', 'max_pnl_pct',
            'entry_slippage_pct', 'buy_latency_ms', 'sell_latency_ms'
        ]
        with open(self.csv_path, 'w', newline='') as f:
            csv.writer(f).writerow(headers)

    def log_trade(
        self, mint: str, entry_curve: float, peak_curve: float,
        exit_curve_decision: float, exit_curve_final: float,
        entry_buyers: int, entry_velocity: float, buyer_velocity: float, token_age_sec: float,
        sells_at_entry: int, largest_buy_pct: float, top2_concentration: float, bundled: bool,
        exit_reason: str, hold_secs: float, peak_time_sec: float, peak_to_exit_sec: float,
        sells_survived: int, invested: float, received: float, max_pnl_pct: float,
        entry_slippage_pct: float, buy_latency_ms: float, sell_latency_ms: float
    ):
        pnl_sol = received - invested
        pnl_pct = ((received / invested) - 1) * 100 if invested > 0 else 0

        # Convert UTC to AEDT (UTC+11)
        utc_now = datetime.now(timezone.utc)
        aedt_offset = timezone(timedelta(hours=11))
        aedt_time = utc_now.astimezone(aedt_offset)

        row = [
            aedt_time.strftime('%Y-%m-%d %H:%M:%S'),
            mint[:8],
            f"{entry_curve:.2f}", f"{peak_curve:.2f}",
            f"{exit_curve_decision:.2f}", f"{exit_curve_final:.2f}",
            entry_buyers, f"{entry_velocity:.2f}", f"{buyer_velocity:.2f}", f"{token_age_sec:.1f}",
            sells_at_entry, f"{largest_buy_pct:.1f}", f"{top2_concentration:.1f}", "Y" if bundled else "N",
            exit_reason, f"{hold_secs:.1f}", f"{peak_time_sec:.1f}", f"{peak_to_exit_sec:.1f}", sells_survived,
            f"{invested:.4f}", f"{received:.4f}", f"{pnl_sol:+.4f}",
            f"{pnl_pct:+.1f}%", "WIN" if pnl_sol > 0 else "LOSS", f"{max_pnl_pct:+.1f}%",
            f"{entry_slippage_pct:+.1f}%", f"{buy_latency_ms:.0f}", f"{sell_latency_ms:.0f}"
        ]

        with open(self.csv_path, 'a', newline='') as f:
            csv.writer(f).writerow(row)

        logger.info(f"📊 Logged: {mint[:8]} | {'WIN' if pnl_sol > 0 else 'LOSS'} | {pnl_sol:+.4f} SOL")
