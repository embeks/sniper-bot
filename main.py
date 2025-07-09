from utils import check_new_tokens, send_telegram_alert, load_wallets_to_follow, check_wallet_activity
import time

# ✅ Run once at start
send_telegram_alert("✅ Sniper bot is now live")
wallets_to_follow = load_wallets_to_follow()

# 🌀 Main Loop
while True:
    try:
        check_new_tokens()
        check_wallet_activity(wallets_to_follow)
        print("Waiting for next scan...")
        time.sleep(60)  # Default interval
    except Exception as e:
        print(f"[!] Loop error: {e}")
        time.sleep(30)
