import asyncio
from sniper_logic import start_sniper

async def main():
    try:
        await start_sniper()
    except Exception as e:
        print(f"[🔥] Sniper crashed: {e}")
        # Optional: add restart logic or alert to Telegram here

if __name__ == "__main__":
    asyncio.run(main())
