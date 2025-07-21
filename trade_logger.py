from datetime import datetime
import csv

def log_trade_to_csv(token, action, amount_in, amount_out):
    with open("trade_log.csv", "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([datetime.utcnow().isoformat(), token, action, amount_in, amount_out])
