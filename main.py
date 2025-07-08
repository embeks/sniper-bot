import time
import requests
import os

# 🔐 Load secrets from environment variables
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🎯 DEGE target flags
dege_10x_hit = False
dege_50x_hit = False
dege_100x_hit = False
dege_300x_hit = False

# Config
MIN_LIQUIDITY = 20000
MIN_HOLDERS = 20
MAX_HOLDERS = 300
CHECK_INTERVAL = 60

def send_telegram_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[!] Telegram error: {e}")

def check_new_tokens():
    global dege_10x_hit, dege_50x_hit, dege_100x_hit, dege_300x_hit

    url = "https://public-api.birdeye.so/public/tokenlist?sort_by=txns24h&sort_type=desc&limit=15"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
    except Exception as e:
        print(f"[!] Birdeye error: {e}")
        return

    for token in data.get("data", []):
        name = token.get("name", "").lower()
        address = token.get("address")
        liquidity = token.get("liquidity", 0)
        holders = token.get("holders", 0)
        price = token.get("price", 0)
        txns_5m = token.get("txns5m", 0)
        chain = token.get("chain", "unknown")

        print(f"Scanning: {name} | Chain: {chain} | Price: {price} | LP: {liquidity} | Holders: {holders} | 5m TXNs: {txns_5m}")

        # 🎯 DEGE PRICE TARGETS
        if name == "dege":
            if price >= 0.002 and not dege_10x_hit:
                send_telegram_alert(f"🎯 DEGE HIT 10x!\nPrice: ${price:.5f}")
                dege_10x_hit = True
            elif price >= 0.01 and not dege_50x_hit:
                send_telegram_alert(f"🚀 DEGE HIT 50x!\nPrice: ${price:.5f}")
                dege_50x_hit = True
            elif price >= 0.02 and not dege_100x_hit:
                send_telegram_alert(f"🏆 DEGE HIT 100x!\nPrice: ${price:.5f}")
                dege_100x_hit = True
            elif price >= 0.06 and not dege_300x_hit:
                send_telegram_alert(f"💥 DEGE HIT 300x!\nFINAL TARGET!\nPrice: ${price:.5f}")
                dege_300x_hit = True

        if chain != "solana":
            continue
        if txns_5m > 100 and liquidity < 1000:
            continue
        if liquidity >= MIN_LIQUIDITY and MIN_HOLDERS <= holders <= MAX_HOLDERS:
            msg = (
                f"🆕 NEW SOLANA TOKEN DETECTED\n\n"
                f"Name: {name.upper()}\nLiquidity: ${liquidity:,.0f}\nHolders: {holders}"
            )

send_telegram_alert("✅ Sniper bot is now live and running on Render.")  # ← outside the function

# Main loop
while True:
    try:
        check_new_tokens()
        print("Waiting...")
        time.sleep(CHECK_INTERVAL)
    except Exception as e:
        print(f"[!] Loop Error: {e}")
        time.sleep(30)
