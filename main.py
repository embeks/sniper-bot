# main.py
import asyncio
from solana_sniper import buy_token
from utils import send_telegram_alert

# ✅ Called once on start
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and ready to snipe")

# 🌀 Main loop (placeholder for real triggers)
async def main():
    await startup()

    while True:
        try:
            # 🧠 Example: hardcoded token to test
            token_to_snipe = "TOKEN_MINT_ADDRESS_HERE"
            await buy_token(token_to_snipe, amount_sol=0.01)

            print("Waiting for next snipe...")
            await asyncio.sleep(60)

        except Exception as e:
            print(f"[!] Loop error: {e}")
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
