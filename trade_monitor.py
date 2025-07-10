# trade_monitor.py
import time
from utils import send_telegram_alert
from price_utils import get_token_price, get_token_liquidity  # These will be added next
from trading import sell_token  # Assumes you have a sell function that sells % of holdings

# Keep track of already sold tokens to avoid double selling
tracked_tokens = {}

# Settings
PARTIAL_SELLS = {
    "2x": 0.33,
    "5x": 0.33,
    "10x": 1.0
}

TIMEOUT_SELL_SECONDS = 300  # 5 mins
RUG_THRESHOLD = 0.75  # If liquidity drops below 75%


def track_token(token_address, buy_price, initial_liquidity):
    tracked_tokens[token_address] = {
        "buy_price": buy_price,
        "initial_liquidity": initial_liquidity,
        "buy_time": time.time(),
        "sells": set()
    }
    send_telegram_alert(f"✅ Tracking token after purchase:
{token_address}\nBuy Price: ${buy_price:.6f}")


def monitor_tokens():
    for token_address, data in list(tracked_tokens.items()):
        current_price = get_token_price(token_address)
        current_liquidity = get_token_liquidity(token_address)
        buy_price = data["buy_price"]

        # --- Check price multipliers ---
        for label, multiplier in [("2x", 2), ("5x", 5), ("10x", 10)]:
            if label not in data["sells"] and current_price >= buy_price * multiplier:
                percentage = PARTIAL_SELLS[label]
                sell_token(token_address, percentage)
                send_telegram_alert(f"💰 Profit target hit ({label})! Sold {int(percentage * 100)}% of holdings.")
                data["sells"].add(label)

        # --- Check timeout sell ---
        elapsed = time.time() - data["buy_time"]
        if elapsed > TIMEOUT_SELL_SECONDS and "timeout" not in data["sells"]:
            sell_token(token_address, 1.0)
            send_telegram_alert(f"⏰ Timeout reached. Sold full position for token {token_address}")
            data["sells"].add("timeout")

        # --- Check rug protection ---
        if current_liquidity < data["initial_liquidity"] * RUG_THRESHOLD and "rug" not in data["sells"]:
            sell_token(token_address, 1.0)
            send_telegram_alert(f"🚨 RUG WARNING — Liquidity dropped! Selling all for {token_address}")
            data["sells"].add("rug")
