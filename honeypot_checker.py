# honeypot_checker.py
import requests
import os
from dotenv import load_dotenv
from utils import send_telegram_alert

load_dotenv()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# 🔍 Basic Honeypot & Rug Risk Checker for Solana Tokens
def check_token_safety(token_address):
    url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}

    try:
        res = requests.get(url, headers=headers)
        data = res.json().get("data", {})

        liquidity = data.get("liquidity", 0)
        buy_tax = data.get("buyTax", 0)
        sell_tax = data.get("sellTax", 0)
        holders = data.get("holders", 0)

        # ✅ Basic filtering logic
        if liquidity < 10000:
            return "❌ Rug Risk: Low Liquidity"
        if buy_tax > 15 or sell_tax > 15:
            return f"⚠️ Possible Honeypot: Buy/Sell Tax too high ({buy_tax}% / {sell_tax}%)"
        if holders < 20:
            return "⚠️ Low Holders: Possibly Inactive"

        return "✅ Token passed basic safety checks"

    except Exception as e:
        return f"[!] Error checking honeypot: {e}"

# ✅ Example (for testing)
if __name__ == "__main__":
    test_token = "So11111111111111111111111111111111111111112"  # Replace with real token
    result = check_token_safety(test_token)
    print(result)
    send_telegram_alert(f"🔎 Token Scan Result:\n{result}")
