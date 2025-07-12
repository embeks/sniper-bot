import asyncio
from utils import (
    check_new_tokens,
    send_telegram_alert,
    load_wallets_to_follow,
    check_wallet_activity
)

async def main():
    send_telegram_alert("✅ Sniper bot is now live")
    wallets_to_follow = load_wallets_to_follow()

    while True:
        try:
            await asyncio.gather(
                check_new_tokens(),
                check_wallet_activity(wallets_to_follow)
            )
            print("✅ Waiting 60s until next check...")
            await asyncio.sleep(60)
        except Exception as e:
            print(f"[!] Loop error: {e}")
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
